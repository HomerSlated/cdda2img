# TODO

## Open

### 🔴 NEXT SESSION

Both raised by kgr at the close of 2026-08-03. N1a and N1b resolved 2026-08-04;
N1d settled and N1c superseded 2026-08-05; **N3, N4 and N5 implemented 2026-08-06**.
**Open: N2, N6 (queued 2026-08-07) and N8 (queued 2026-08-10). N7 answered 2026-08-11.**
**Reconciled 2026-08-12** — N2 is down to `progress-map-plan.md` §6 steps 5–6 (the
Q lane and the track marker both shipped 2026-08-08); N6's prior question is
answered by kgr and the item is unblocked but unimplemented; N7's residue is a
policy call. N8 is unchanged. (N1e is withdrawn
in full — retained below as evidence, not a plan. Its deletion list must not be
actioned.)

N5's remaining work is validation, not code: the alternatives menu is covered by
fixtures built to the reference disc's measured shape, but every container on the
shelf is the same album, so a differing tie size and a blank `disambiguation` have
never been exercised on real hardware. kgr's step 8 stands.

#### ~~N1a. Tracy Chapman track 8 no longer reports a failure — is the repair now
invisible, or did it not happen?~~ — **RESOLVED 2026-08-04. It did not happen.**

The hypothesis was that with analysis *and* repair both outsourced to AccuDisc the
operation runs silently, so we never see the error or the repair. Two further rips
on 2026-08-04 refute it, and they do so on **both** exit paths of the recovery
ladder — track 8 failed AccurateRip on each, and each left a complete trace:

| container | created | `ripper` | track 8 |
|---|---|---|---|
| `Tracy Chapman.rbi` | 08-01 10:19 | `accudisc+ctdb` | `ctdb_repaired@67116` |
| `Tracy Chapman_1.rbi` | 08-03 19:03 | `accudisc` | no failure, no `ctdb_*` keys |
| `Tracy Chapman_2.rbi` | 08-03 19:10 | `accudisc` | no failure, no `ctdb_*` keys |
| `Tracy Chapman_3.rbi` | 08-04 15:37 | `accudisc+c2rec` | `ctdb_declined=CTDB parity fetch failed` → `matched@24X` |
| `Tracy Chapman_4.rbi` | 08-04 15:44 | `accudisc+ctdb` | `ctdb_entry=67116`, `ctdb_offset=-639`, `ctdb_erasures=c2` → `ctdb_repaired@67116` |

So **AR is still able to fail**, the gate still fires, and neither exit is silent.
The 08-03 pair were genuinely clean reads — candidate (a), which the recovery
bench's own finding (read errors are stable per *speed*, not per defect) already
made unremarkable. Candidate (b), a defect upstream of the gate introduced by the
migration, is dead: the gate fired twice on 08-04 against migrated code.

Rip 3 is the more valuable of the two, because it exercised a path nothing had yet
covered end to end: CTDB unreachable → decline recorded → speed ladder → match at
24×. Worth keeping in mind as a known-good reference for that arm.

The reasoning that got here is worth keeping: absence of *every* `ctdb_*` key means
no attempt, because a declined attempt writes `ctdb_declined` plus the entry and
offset. That asymmetry is what let a negative result be read off a container
without a rerun — it is only as good as the discipline of always recording the
decline, so do not let that emission get "tidied up".

**Loose thread, not chased:** `ctdb_offset=-639` on 08-04 against the `-669`
recorded earlier, differing by exactly the +30 read offset. Consistent with the
repair now running in the raw domain, but that is a hypothesis, not a measurement,
and conflating those two offset domains is a trap already fallen into once
(`project_ctdb_offset_domains`). Measure before believing it.

#### ~~N1b. `lookup_status_discogs=empty` beside `discogs_barcode_corroborates=YES`~~ — **EXPLAINED 2026-08-04; one design item remains (N1c)**

The two keys were never contradictory: they come from different queries. The
corroboration path follows the **MB** release's `inc=url-rels` to a Discogs
*release id* and fetches that release; `lookup_status_discogs` reports a Discogs
barcode *search*. Both can be true at once. Three defects were found behind the
appearance, all fixed:

1. **`lookup_status_discogs` was reporting on MusicBrainz.** `has_data` was
   `disc.barcode != pre_discogs_barcode`, but phase A of
   `_prepopulate_from_discogs` writes the barcode from **MB's** hints *before*
   Discogs is queried, so on any disc where MB supplied it there was no delta and
   the key read `empty` whatever Discogs returned. Now `discogs_hit is not None`,
   the signal the function already returned and was discarding.
2. **`search_by_barcode` truncated to `page(1)[:25]`.** A barcode search is not
   ranked, so that was an arbitrary 25 of however many exist. Tracy Chapman's
   barcode yields **68** releases and the one MB links to was not among the 25.
   Now paginated to a 4-page cap.
3. **The corroboration key overstated its claim** and is renamed
   `discogs_corroborates` / `discogs_barcode_conflict`. An MB editor
   supplies both the barcode and the relation, so agreement validates *the link*,
   not the pressing. Disagreement is the useful direction.

#### ~~N1c. **[C2I] Make the MB→Discogs link the primary Discogs path, search the fallback**~~ — **SUPERSEDED by N1e 2026-08-05.** Both consumers of the reorder are being deleted. The measurements below stand as evidence and are cited by N1e; do not re-derive them.

kgr's design, 2026-08-04: we already hold a url-relation to exactly one Discogs
release and use it only for a barcode check, while the metadata path runs a
search that cannot resolve one. Invert that — link first, `search_by_barcode`
only when MB has no Discogs relation.

**Evidence gathered, do not re-derive.**
- **Link coverage**: 19/24 (79%) on a random GB official-CD sample; near 100% on
  a commercial-album shelf, per kgr's experience. Both readings agree the link is
  the primary path and the search is a real fallback, not dead weight.
- **Search cannot substitute for the link.** Ground truth is the MB-linked
  release (Discogs 6646745 for MB 65e67d39). Scoring the 68 candidates on
  title/artist/year/barcode/country ranks the truth **28th**, with five *wrong*
  candidates tied above it. Adding catalogue number and dropping year lifts it
  into the top tier but leaves an **8-way tie**. 16 of the 68 share the exact
  catalogue number.
- **Why, structurally**: you cannot disambiguate on the field you searched by,
  nor on fields near-constant within its results. Barcode *is* the query; album
  and artist follow from it; country and cat# vary only coarsely. What separates
  these pressings on Discogs — matrix/runout, plant, sleeve variant — is not in
  the search stub. Also **44 of 68 carry no year**, so a year test scores absent
  data as disagreement and penalises the correct answer.
- **One barcode, 68 releases, 12 countries, 12 catalogue numbers.** A UPC
  identifies a retail product line, not a manufacturing run. It is a good
  *filter* and a poor *key* — the same conclusion §1a reached about the on-disc
  MCN, arrived at from the service side.

- **A barcode can be forged, not merely shared.** Of the 68, **one is a different
  artist entirely**: Discogs 18528415, Jamiroquai *The Best 2000*, UK 1994,
  carrying `075596077422` on its printed sleeve. It is not a submission error —
  and it is not Sony reusing Elektra's UPC either. Discogs classifies it
  `formats[].descriptions = ['Compilation', 'Unofficial Release']` with no
  catalogue number: a bootleg that copied a real barcode. This is a *different*
  failure from the other 67. Breadth is sloppiness and leaves every hit the right
  album; forgery is adversarial, and a trust model can price in "this key is
  coarse" but not "this key can be faked" without an independent second check.

**Open questions.** (a) For the fallback branch, `cddb.consensus_from_candidates`
is the shape that fits — merge only the fields a tied set agrees on, rather than
picking one arbitrarily. (b) `_albums_match` must still gate the *linked*
release. **Settled by the bootleg above**: a mis-linked relation and a forged
barcode deliver the same wrong object, and "MB pointed at it" is not a substitute
for checking what arrived. (c) Resolve the link once and pass it to both
consumers rather than fetching url-rels twice. (d) **Drop unofficial releases**
from barcode results — Discogs marks them in `formats[].descriptions`, which
`_parse_result` currently discards (`DiscMeta` has no format field). Cheap, and
it removes the one adversarial candidate before any scoring runs.

**Caveat**: the disambiguation numbers are n=1, on a heavily-reissued album. The
structural argument does not depend on the sample; the *rate* does.

**N1c may be overtaken by N1d below — read that first.** kgr's position at the
close of 2026-08-04 questions whether Discogs should be a corroboration source at
all, which changes what the reorder is *for*.

#### ~~N1d. **[DESIGN, kgr thinking] What is Discogs actually for?**~~ — **SETTLED 2026-08-05. Delete the automatic paths, keep the interactive one. See N1e.**

kgr's position, 2026-08-04, recorded rather than actioned. **No code until this
settles**; it governs N1c.

**The argument.** Discogs is user-submitted and, as measured above, riddled with
errors, missing data, inconsistencies and unexpectedly with pirate CD-Rs. It is
genuinely useful for a large class of facts we never record in PROV — sleeve
variants, pressing plant, matrix/runout, jewel-case detail — but *none of those
exist in the disc data*. They live on the outside of the disc and on the insert,
so they can only be read by a human holding the case, and therefore **none of
them can disambiguate anything programmatically**.

That makes Discogs a poor corroboration source. The only thing we can actually
lean on is the Discogs link presented on the MB release, and that confirms
exactly one thing: a human edited that MB page and chose to associate it with one
of many Discogs pages. **That is not independent corroboration.** And a
reciprocal Discogs→MB link would not fix it — independence is not about which
page holds the pointer, it is about whether the two records come from different
populations. They do not: same community, often the same editor. So
`discogs_corroborates` still overstates even after today's rename; the
honest reading is "two user-submitted records that a human deliberately
associated do not contradict each other". Better characterised as *additional
info that may or may not be accurate* than as corroboration.

kgr's conclusion: the only real corroborator and disambiguator in this pipeline
is **the user**, who has the physical disc in hand and can see the many
identifiers that were never baked into the disc data.

**One distinction worth keeping when this is picked up** (agent's contribution,
not kgr's): "user-submitted" is not one category. The MB disc ID, AccurateRip and
CTDB are all crowd-contributed, yet nobody can submit a wrong disc ID that still
matches, because the TOC recomputes it — and a wrong AR checksum does not survive
contact with 200 independent rippers, because agreement there is arithmetic
rather than opinion. A typed catalogue number has no such property: nothing
recomputes it, an error propagates forever, and a bootlegger can forge it. The
axis that matters is therefore **derived from the artefact vs asserted about it**,
not user-submitted vs authoritative. By that split the pipeline already holds
real authority — disc ID, AR, CTDB, pressed ISRC/MCN — just not anywhere Discogs
operates.

**Candidate directions, none chosen.**
- **Keep the link as a *destination*, not as evidence.** `discogs_release_id` is
  already in PROV; its durable value is that it takes the user one click from the
  container to the page with sleeve photographs they can hold the disc against.
  Under that reading the barcode comparison is only interesting when it *fails*,
  as a hint the pointer is bad.
- **PROV should record provenance, not verdicts.** `..._agrees=YES` still reads
  as a confidence claim. Recording the fact (the link id, and both barcodes when
  they differ) and letting a reader conclude is more honest and ages better —
  the same reason `ctdb_declined` records what happened rather than whether it
  was acceptable.
- **Or retire §10.3.1.** If the output is "may or may not be accurate", it should
  either be a recorded fact or not cost two network round-trips. Today's rename
  was the minimum honest change, not necessarily the right one.
- **The product work may be in the menu, not the lookups.** Presenting ambiguity
  well — "13 candidate pressings, here is the Discogs link, you have the disc" —
  rather than trying to auto-resolve what only a human with the jewel case can
  settle.

Relates to [[project_metadata_authority_model]] ("Guess the Album": no
authoritative ground truth, every field a best guess, the user is final arbiter,
PCM verbatim is the real guarantee) — this is that position re-derived from the
Discogs side, which suggests the architecture is already right and only the
labelling overclaimed. Also feeds OPT-4 and
`docs/reference/identifier_trust_model.md` §1a, whose justification for keeping
the service barcode as *the* disambiguation key rests on a cleaner-namespace
argument that today's measurements do not support.

#### N1e. **[C2I] Demote Discogs to an interactive-only source** — **STAY OF EXECUTION 2026-08-05, superseded in scope by N5**

> **DO NOT ACTION. The deletion list below is WITHDRAWN IN FULL** (kgr, later the
> same day). Both AcoustID and Discogs **stay**, entirely unchanged — "the rest of
> the behaviour remains unchanged". Nothing in `discogs_lookup.py`,
> `_prepopulate_from_discogs`, R11 or §10.3.1 is to be removed. **Read N5**, which
> supersedes this item and is additive only.
>
> This entry is retained **as evidence, not as a plan**. Its measurements bound
> what the retained code contributes (Discogs supplied 0/18 values MB lacked; R11
> differed 0/8) and they remain the reason nobody should later cite the retention
> as proof the merge was validated. They are not a reason to delete anything.

Settles N1d and **supersedes N1c** (there is nothing to reorder once no automatic
consumer remains). kgr proposed deleting Discogs outright; the agent was asked to
argue against, went looking for a defence, and found the measurements went the
other way except on one point. Agreed split: **delete every automatic path, keep
the interactive screen.**

**The measurement that decided it** (2026-08-05, 10 albums → 20 MB releases,
18 with a Discogs link; each release compared field-by-field against the release
its own MB→Discogs url-relation points at). B-6's premise was that Discogs
outranks MB on the catalogue fields; it does not.

| field | both populated | agree | **Discogs supplied a value MB lacked** |
|---|---|---|---|
| `catalog_number` | 18/18 | 16 | **0** |
| `label` | 18/18 | 14 | **0** |
| `country` | 18/18 | 2 | **0** |

The disagreements are naming convention, not correction: `Polar` vs `Polydor`
(imprint vs distributor, both true), `EMI Music Canada` vs `Parlophone`,
`DGC Records` vs `DGC`, `422 828 553-2` vs `P2 28553` (label number vs disc
number). The `country` column looks catastrophic only because MB returns ISO
alpha-2 and Discogs returns free text — and that is a **defect on the keep side**:
`discogs_lookup._parse_result` does `country=str(country)` with no ISO mapping, so
`"Europe"` / `"Australasia"` / `"UK"` can reach `RBIDisc.country`, which the spec
documents as alpha-2. Narrow (the merge is fill-blank, so MB wins whenever MB has
a value, and phase B needs `len(results)==1`) but real.

**R11 likewise**: `lookup_master_year` is the only automatic Discogs path that can
*change* a stored value (on disagreement it prefers the earlier year and writes
it). Over 8 comparable albums: **8 same, 0 different, 2 no-data.** Caveat, stated
so nobody over-reads it: 0/8 puts the upper bound on the disagreement rate near
30%, and the sample never exercised the case R11 exists for — MB returned a
visibly wrong `first-release-date=2003` for *Dark Side of the Moon* and Discogs
returned `None` there. "No observed benefit", not "no benefit".

**Sample caveat**: ten famous Western releases is MB's strongest ground, so
"Discogs fills no gaps" is weakest on obscure or regional pressings. The structural
counter is that the gap-filling case is **unreachable by construction** — no MB
release means no `mb_release_id` and no barcode hint, phase A picks nothing, and
phase B never runs. Discogs is only ever asked about discs MB already answered.

**What survives, and why.** Not corroboration — *addressing*, for the only
verifier in the pipeline. N1d concluded the user holding the disc is the sole real
disambiguator; a disambiguator needs candidates and needs evidence printed **on
the object** (matrix/runout, sleeve variant, plant, catalogue number). That is the
class kgr dismissed as trivia, and he is right that we do not store it and no
machine can check it — but it is the only data anywhere in this system that a
human can hold against a jewel case. Everything else we trust (MB disc ID, AR,
CTDB) is checkable only against the *data*, which is exactly why it cannot settle
which of 68 pressings is in your hand. `DiscogsSearchScreen` weighs nothing,
asserts nothing, and writes nothing the user did not pick.

**Delete** (automatic, machine-weighed):
- `_discogs_barcode_corroborate` (§10.3.1) and both PROV keys
  (`discogs_corroborates` / `_conflict`).
- `_prepopulate_from_discogs` **phase B only** — the barcode search + merge.
  **Phase A survives untouched**: the canonical-barcode pick is MB-hint driven and
  never calls Discogs. Its return signature loses `applied_hit`.
- `lookup_master_year` (R11) and `_r11_corroborate_with_discogs_master`.
- The B-6 catalogue trust override (`_FIELD_TRUST_OVERRIDE`, `_CATALOGUE_FIELDS`)
  and `Source.DISCOGS` / `Trust.DISCOGS` from the resolver. The descending-trust
  invariant survives deleting one level intact — it is a total order either way.
- `lookup_status_discogs`.

**Keep**: `DiscogsSearchScreen`, `search_releases`, `search_by_barcode`,
`fetch_release`, `is_available`, `normalize_barcode` (already lives in
`barcode.py`), and `discogs_release_id` in PROV — the one-click route from
container to sleeve photographs. Roughly 150 of `discogs_lookup.py`'s 313 lines.
Note `primary_type` was already interactive-only: its sole consumer is the results
table at `metadata_menu.py:237`.

**Do not lose on the way through**: `_albums_match` is what stopped the forged
barcode (Discogs 18528415, a bootleg carrying a real UPC) reaching metadata. If
phase B goes, the guard goes with it — but if any future path re-merges a Discogs
release automatically, it needs that check back.

If kgr later prefers cutting the menu screen too, the deletion commit must record
that it removes the user's only route to sleeve evidence, so a future session does
not rebuild it believing it is new.

**Decisive addendum, measured 2026-08-05.** kgr asked whether MB's §10.3 cascade,
applied to a Discogs barcode search, could reach one candidate. **It can — and
that is the argument against it, not for it.** Run verbatim over the 68 results:

| rung | survivors | why |
|---|---|---|
| `search_by_barcode` | 68 | — |
| `_is_consistent` (MCN/ISRC) | 68 | **inert** — 0/68 carry any ISRC |
| R1 ISRC tally | 68 | **inert** — nothing to tally |
| plurality release-group | 68 | **inert** — search stubs carry no master id |
| `barcode_plurality` | 68 | **structurally inert** — the barcode *is* the query |
| `preferred_country` GB,XE,US | **1** | exactly one row is country `UK` |
| `date`, `id` | 1 | already unique |

**The single survivor is Discogs 18528415 — the Jamiroquai bootleg.** It wins
because `GB` outranks `XE`, and of 68 rows exactly one is `UK`. The cascade
converges on a different album by a different artist, with **no tie**, so every
confidence signal reads maximally decisive. Country histogram for the record:
Europe 24, US 22, Brazil 6, Canada 5, Mexico 3, Australasia 2, Israel/Chile/
Venezuela/Netherlands/UK 1 each.

**Root cause — a precondition Discogs cannot meet.** `_select_release_lexicographic`'s
docstring states it: *"candidates must already be the album-consistent set (the
plurality release-group), so this only chooses the pressing, never the album."*
MB satisfies that one rung earlier (all 7 candidates shared one RG). On Discogs
that rung is inert, so a **preference** rung runs on a population whose album
identity was never established. `preferred_country` was never an identifier; it
is only safe downstream of an identity gate.

**With the album gate restored** (`_albums_match`, the guard N1c open question (b)
was about): 68 → 67 (bootleg dropped) → 24 (Europe) → **5** (year 1988) → 1 by the
terminal id tiebreak. Winner Discogs **770495**, cat `960 774-2`. Ground truth is
**6646745**, cat `7559-60774-2` — **eliminated at the `date` rung**, because it
carries no year and 19 of those 24 candidates carry none either. The rung converts
*missing data* into *elimination*, and the truth is among the missing. This closes
N1c open question (d) harder than filtering unofficial releases would: the bootleg
is stopped by `_albums_match`, not by a format filter.

**Three conclusions.**
1. Reaching one candidate is **not** evidence of having identified it. Both
   services end at an arbitrary tiebreak — MB 5→1 on `mbid`, Discogs 5→1 on `id`.
2. Discogs is strictly worse only because it lacks the album gate, not because its
   data is dirtier. Restore the gate and it fails the same way MB does.
3. The two cascades **disagree** even when both succeed: Discogs 770495 carries
   cat `960 774-2`, which matches MB candidate `7531d07c` — *not* the `65e67d39`
   MB picked. Two arbitrary tiebreaks over two indistinguishable pools have no
   reason to agree, and here they do not.

**kgr's reframe, 2026-08-05 — don't search at all; the disc ID already bounded it.**
This supersedes the whole cascade question and reshapes the keep half of N1e.
Measured on the same disc:

- The barcode search population is **68**; the disc-ID candidate set is **7**. The
  barcode was always the wrong population — *everything sharing this UPC* rather
  than *everything matching this TOC*. An order of magnitude, obtained by using a
  key **derived from the artefact** instead of one **asserted about it**.
- **The bootleg cannot appear.** Discogs 18528415 does not share the TOC. The
  disc-ID population is intrinsically album-consistent, so it satisfies
  `_select_release_lexicographic`'s stated precondition *by construction* — no
  `_albums_match` guard needed, because the fingerprint already guarantees it.
- **7/7 of the disc-ID candidates carry an MB→Discogs link** on this disc (against
  the 79% general sample in N1c). All five XE pressings resolve into the 68, so the
  links are real and mutually consistent: `592306`, `11630069`, `15382069`,
  `6646745`, `21719110`.

**Consequence for the keep/delete split**: the primary Discogs path is
`mb_lookup.discogs_link_and_barcode` — which lives in **`mb_lookup.py`, not
`discogs_lookup.py`**. So the link route needs no part of `discogs_lookup` at all.
What that module would still add is optional per-row enrichment (thumbnail, format,
`fetch_release`) and the user-driven menu searches. `search_by_barcode` loses its
last automatic justification entirely.

**Cost**: one url-rel call per candidate at MB's pinned 1 req/s (R15) — ~7 s here,
and only on a multi-match. Fetch lazily per row and pagination becomes free.

#### B-7 design input, measured 2026-08-05

kgr's proposal: feed the per-candidate MB `disambiguation` strings into the
alternatives menu, since that is the only field distinguishing otherwise-identical
pressings, and paginate when the list is long. Three measurements bear on it.

0. **Use MB's `annotation`, not `disambiguation` — decided by measurement 2026-08-05.**
   `disambiguation` is a **lossy summary**. For `b63ffa5b` it reads *"WE 835, newer 'e
   above E' Elektra logo on disc"* — it **drops the word "France"**, which is the single
   token that identified kgr's disc. The annotation carries it: *"price code '''France
   WE 835''' on back"*. Annotations also carry matrix codes, Mastering/Mould SID codes,
   the manufacturing plant and glass-master dates — a far stronger discriminator than a
   logo variant, and all of it readable off the disc and inlay by hand. Coverage is
   **6/7** here, and it is a separate MB include (`includes=["annotation"]`) that
   nothing in the codebase fetches today. Practical note: annotations are **wiki
   markup** (`'''bold'''`, `[url|label]`, `== headings ==`) and need stripping or
   rendering before a TUI can show them.
1. **Coverage is good but not total — 6/7 carry a disambiguation string, 1 is
   blank.** The blank row (`e9b905e6`, AU) is unpickable as designed: every other
   column is identical across candidates. It is also, usefully, **the only
   candidate carrying a date** (`1988-04`). So the row template needs a fallback
   chain — `disambiguation` → date / country / catalogue number → bare MBID —
   rather than a single field. A blank row must never render as an empty line.
2. **List size is bounded by TOC-sharing, not barcode-sharing** — 7 here, not 68.
   That is the reason to expect the list to stay tractable, but it is **n=1**: a
   heavily-reissued album should be measured before paging is designed around an
   assumption. Do not generalise from this disc.
3. **The rows are one click from sleeve photographs.** Pairing the MB
   disambiguation text (what is printed on the disc) with the Discogs link (photos
   of exactly that) gives the user both halves of the comparison against the object
   in their hand. This is the concrete form of N1d's "the only real corroborator is
   the user" — the menu stops trying to decide and starts equipping the person who
   can.

#### ~~N3. **[BUG, measured live 2026-08-05] AcoustID corroboration is broken by a 25-result truncation, and the §10.4 gate can never fire**~~ — **FIXED 2026-08-06 (`591e5f4`).**

> Shipped as described below, with one correction to the plan: the browse is an
> **addition**, not a replacement. `get_recording_by_id` is still needed for the
> recording-level fields (title, artist credit, ISRCs) that a release browse does
> not return, so it is two requests per recording, its includes reduced to
> `["artists","isrcs"]`. Verified live on the reference recording: 25 → 43
> releases, release-group 0/43 → 43/43, and **both** the pinned release
> `65e67d39` and kgr's actual pressing `b63ffa5b` present where neither was
> before. Through the real R6 path on `Tracy Chapman_6.rbi`,
> `acoustid_corroborates` flips NO → YES; the gate now sees 13 distinct
> release-groups and reaches a verdict (PASS). Paging turned out to be the
> load-bearing half — a one-page walk reproduces the same defect at 100 instead
> of 25 — so the page cap logs when it binds. The stale docstrings are corrected,
> and `_r6_tally_and_merge`'s name is now flagged in its own docstring as
> historical (it merges nothing on either branch).


Two defects, one root cause, one fix. Found by tracing R6 end-to-end against
`Tracy Chapman_6.rbi`. **The same 25-result truncation shape as the Discogs
`search_by_barcode` bug fixed 2026-08-04 — a different service, the same failure.**

**Symptom.** Every container on the shelf records `acoustid_corroborates=NO` with
`lookup_status_acoustid=OK`, `match_confidence=0.300`, `match_recommendation=low`.
The `NO` is a **false negative**.

**What actually happens** (all hops verified live, not inferred):
1. Fingerprinting is healthy. Track 1 scores **0.978**, track 6 **0.974**.
   `/bin/fpcalc` *is* broken on this machine (`Could not create an audio converter
   instance`, exit 2, empty stdout — FFmpeg 6.x SwR bug) but **pyacoustid never
   calls it**; it goes through libchromaprint directly. The old "fpcalc breaks the
   live AcoustID step" note is **wrong** and has been corrected in memory.
2. AcoustID returns the **correct** recording for both tracks — our release's own
   track-1 recording `fd3158d6` is in the returned set.
3. `_chain_to_mb` then calls `get_recording_by_id(..., includes=[... "releases" ...])`.
   **MB returns 25 releases while `release-count` says 43.** Our release
   `65e67d39` is in the 18 that were cut.
4. So track 1's fan-out (32 distinct release ids over 4 duplicate recording
   entities) does not contain the disc's own release, track 6's 21 does, and
   `selected_release_id in consistent_rids` is False → `NO`.

**Second defect, same call.** `_acoustid_gate` matches at release-group level, but
under `inc=releases` the release dicts have **no `release-group` key at all** —
measured 0/32 and 0/21 populated. `rg_seen` is therefore always empty, the
`if rg_seen and ...` guard always short-circuits, and **the gate has never fired
on any disc.** The comment at `acoustid_lookup.py:160-168` calls this an "empty
stub"; it is actually an absent key, and it says a per-release follow-up is "one
extra request per row" — the browse endpoint below makes that cost wrong too.

**The fix — one endpoint change resolves both.** Replace the recording endpoint's
`inc=releases` with a paged `browse_releases(recording=..., includes=["release-groups","media"], limit=100)`:

| | `inc=releases` | paged `browse_releases` |
|---|---|---|
| releases returned | **25 of 43** | **43 of 43** |
| disc's own release present | **NO** | **YES** |
| `release-group` populated | **0/43** | **43/43**, and it matches the disc's RG |
| `medium-list` (Trk column) | present | **43/43** |

So corroboration becomes possible, the gate becomes able to fire, and the Trk
column survives. Cost is one request per *recording* plus one per extra page,
against today's single request — MB is rate-limited to 1 req/s (R15), and R6
fingerprints only 2 tracks with `_MAX_RECORDINGS=5`, so the worst case is bounded.

**Impact on scoring.** `acoustid` contributes `+0.25`. Today this disc scores
0.300 (`mb_disc_id_multi` alone) → **LOW**. With the corroboration it would score
0.550 → **MEDIUM** (`_MEDIUM_THRESH=0.40`). The truncation is costing a confidence
bucket on every rip.

**Do not "fix" this by widening `_MAX_RECORDINGS`** — the cap is on *recordings*,
and the truncation is on *releases per recording*. They are different axes; raising
the cap costs requests and moves nothing.

**Watch for a false pass when validating.** `acoustid_gate` is a fail-only key, so
its absence means "pass **or** not-evaluated". Before the fix it was structurally
always the latter. A test that asserts the key is absent therefore passes today for
the wrong reason and will keep passing after the fix — assert `rg_seen` is
non-empty, not that the gate stayed quiet.

**Fix the stale docstring in the same pass.** `_r6_acoustid_corroborate`
(`cdda2img.py:1573-1577`) still claims *"When the disc has no MB release MBID yet
AND AcoustID converges consistently … merge that release into the disc via
`_merge_into_disc`."* **That merge does not exist.** `_r6_tally_and_merge` has three
`return disc` and zero merge calls; the merge was deliberately removed, and the code
comment one line below records why ("merging album-level metadata here routinely
picks the wrong compilation when MB disc-ID found nothing"). `resolver_adapter.py`
depends on AcoustID contributing no proposals and asserts it by test. So the
docstring describes behaviour the code deliberately does not have — and it is the
first thing a reader consults when deciding whether AcoustID can write metadata.

#### ~~N4. **[BUG, measured live 2026-08-05] `release_selected_via` names the first rung that *varies*, not the rung that *decides***~~ — **FIXED 2026-08-06 (`174a63a`).**

> Option (b) shipped: `release_tied_after=<rung>:<n>` alongside `via`, plus
> `menu_candidates` for N5. **One correction earned by a live run.** The count
> must NOT be computed by asking whether each key varies across the candidate
> set — that is how `via` is defined and it is exactly why `via` misleads. The
> AU pressing is the only one of the seven carrying a date, so `date` varies
> across the whole set while narrowing nothing (country eliminates that row one
> rung earlier and the five survivors are all dateless), and the whole-set test
> answers `date:5`. The first implementation did precisely that and only the
> live MusicBrainz response caught it; the unit fixture had no dates at all. It
> now walks the ladder progressively, asking at each key whether the *surviving*
> set shrank, and the regression fixture carries the AU date. Live:
> `release_tied_after=preferred_country:5`, matching the trace below exactly.


Found while walking the MB disc-ID path end to end on `Tracy Chapman_6.rbi` at
kgr's request. The container records `release_selected_via=preferred_country`.
**Preferred-country did not select the winner** — it narrowed 7 candidates to 5
and left a 5-way tie that the terminal `mbid` tiebreak broke alphabetically.

Full trace, live:

| stage | candidates | what it did |
|---|---|---|
| `lookup_disc_id` (one GET, exact TOC fingerprint) | **7** | — |
| `_is_consistent` (Unit G, MCN/ISRC gospel) | 7 | **0 rejected** — nothing to contradict |
| `_resolve_multimatch` (R1 ISRC tally) | 7 | **no winner** — every candidate scores 11/11 |
| `_plurality_release_group` | 7 | **unanimous** (7/7 share one RG) — no narrowing |
| ladder: `barcode_plurality` | 7 | **unanimous** (7/7 share `0075596077422`) — no narrowing |
| ladder: `preferred_country` GB,XE,US | **7 → 5** | drops FR and AU; **5-way tie remains** |
| ladder: `date` | 5 | all five have **no date** (`9999`) — no narrowing |
| ladder: `mbid` | **5 → 1** | `65e67d39` wins because `6` < `7` < `8` < `b` < `e` |

`_select_release_lexicographic`'s docstring claims *via* is "the key that put the
winner ahead of the runner-up… that key is what decided the ranking". It is
actually the first key with **any** variation across the whole candidate set.
Those coincide only when the first varying key is also uniquely determining, which
is not this disc. Verified by counterfactual — the winner is stable under
`['GB','XE','US']` and moves to `e9b905e6 [AU] via='date'` under `[]` or
`['GB','US']` — so the country rung *is* load-bearing (it eliminates AU, which
would otherwise win on date). It eliminates; it does not select.

**The honest answer to "how did we get down to one candidate" is that we never
did.** We got to five and took the alphabetically-first MBID.

**Why the five cannot be separated** — MB's own `disambiguation` field is the only
thing that differs, and it is free text describing print on the physical object:

```
b63ffa5b  WE 835, newer 'e above E' Elektra logo on disc
8e5e097d  WE 851, no red circle around CD rim, newer "e above E" Elektra logo
e6676f25  WE 835, old Elektra logo and new cat# on disc, approx. 1994/95
65e67d39  EW 835, upper-case "MADE IN GERMANY" on disc, 1999+     <-- picked
7531d07c  WE 851, old Elektra logo, short cat# on disc and inlay, WME matrix
```

Same barcode, same catalogue number (bar `7531d07c`), same label, same country,
same status, no dates. **This is N1d's Discogs conclusion holding for
MusicBrainz**: what separates pressings is readable only by a human holding the
disc. It is not a Discogs-quality problem, it is a property of pressings. Note
this cuts *both* ways for N1e — it weakens "Discogs is uniquely unreliable", and
strengthens "no service can disambiguate this, so the user must".

**FALSIFIED AGAINST THE PHYSICAL DISC, AND RESOLVED, 2026-08-05.** kgr read the mark
on his copy — **`FRANCE WE 835`** — and then found the matching MB annotation. The
disc is **`b63ffa5b`**: *"This release has price code '''France WE 835''' on back and
a '''newer 'e over E' Elektra logo''' on disc."*

The pinned release `65e67d39` carries price code **`EW 835`**. Wrong. So the
alphabetical `mbid` tiebreak picked wrong and has been recording the wrong pressing
in **seven containers** — no longer a 1-in-5 argument but a measured miss, with the
true answer known.

**Two corrections to the agent's own reasoning, recorded because both are traps:**
1. **`WE 835` is a *price code* printed on the back of the inlay — not a matrix or
   mould code.** The agent read it as a matrix prefix. It is a Warner pricing
   marking, and it is not in the runout at all.
2. **The claim "wrong on two independent marks, including country of pressing" was
   itself wrong.** `b63ffa5b` was *also* manufactured in Germany (Cinram, Alsdorf,
   glass masters 2004–2007). `FRANCE` denotes the **pricing territory**, not the
   plant. The refutation rests on **one** mark, the price code — which is sufficient,
   but the second "independent" mark did not exist. This is the same
   territory-vs-plant conflation flagged for `928588a5` one paragraph later, made by
   the agent in the very act of flagging it.

**The right answer was in MB's seven after all**, so the "incomplete candidate list"
worry raised here is not evidenced on this disc. The `rejected` menu state remains
justified on its own terms (a user must never be forced to endorse a wrong row), but
it is a safeguard, not a demonstrated need.

**Design consequence for N5 — the alternatives menu needs a "none of these"
option.** Without one, a menu presented to a user whose pressing is absent forces
them to endorse a row that is wrong, converting an honest `auto_tiebreak` guess into
a false `manual` confirmation — strictly worse provenance than not asking. That
outcome needs its own PROV state alongside the three in (b) below.

**Scope of the defect — the confidence score is already honest.**
`match_distance` only tests `prov.get("release_selected_via")` for *presence*, and
awards `mb_disc_id_multi = 0.30` (not `mb_disc_id = 0.50`), correctly recording
that the pressing is a best guess. The value is display/audit-only. So this is a
**labelling** bug, not a scoring bug — but the label is exactly what a reader
consults when auditing why a pressing was chosen.

**Fix options** (not chosen): (a) report the rung that reduced the set to one —
the *last* discriminating key, which would read `mbid` here and be honest about
arbitrariness; (b) emit both, e.g. `release_selected_via=preferred_country`
plus `release_tied_after=preferred_country:5`, so the tie size is visible; (c)
keep *via* and add `release_candidates=7`. Option (b) carries the most
information for an auditor and matches the `ctdb_declined` precedent of recording
what happened rather than a verdict.

#### ~~N5. **[DESIGN, kgr 2026-08-05] Pressing selection: auto keeps the tiebreak, no-auto opens the alternatives menu**~~ — **IMPLEMENTED 2026-08-06.**

> Shipped: `menu_state.PressingScreen` / `PressingDetailScreen`, reached from the
> main menu as `[s]` and offered **only** when more than one candidate survives
> the last evidence rung. The ladder split is as designed — `preferred_country`,
> `date` and `mbid` do not narrow the menu, so the reference disc shows seven
> rows, not five. Four PROV states (`release_selection`), MB's free text in
> `release_disambiguation`, and `corroborated_release` when a manual change
> leaves the pre-menu corroboration keys describing a different release.
>
> **Three corrections to the design as written.**
>
> 1. **The full ladder still pins a provisional winner on both paths.** Not
>    pinning on no-auto would silently disable four downstream checks (§10.3.1
>    Discogs, R6, stage-7's gate, R9) in order to defer one choice. The split is
>    about what the menu *displays*; the menu overrides the pin.
> 2. **The menu trigger is not `release_tied_after`'s `n`.** It is the size of
>    the post-evidence-rung set. A country rung narrowing 7 → 1 leaves `n` at 1
>    and would suppress a menu that should have shown 7. Two counts, not one.
> 3. **`annotation` cannot ride the disc-ID lookup.** `musicbrainzngs` lists it
>    in `VALID_INCLUDES["discid"]` and the `/release` endpoint accepts it, but
>    the `/discid` endpoint answers **HTTP 400** — measured, after the agent had
>    written a comment claiming it was "verified against a live response" on the
>    strength of having checked the *neighbouring* endpoint. Same shape as the
>    F-003 `discids` regression, and a guard test now pins it. Annotations are
>    fetched lazily, one release at a time, from the detail screen.
>
> 4. **The menu OPENS; it is not an entry the user must find.** kgr's wording
>    is that no-auto "will activate the alternatives menu". A `[s]` item on
>    the main menu would not: `[a]` is the first option and the common action
>    on a disc that looks right, so the alternatives would go unseen and the
>    container would record `auto_tiebreak` — documenting the failure instead
>    of preventing it. `PressingScreen` is therefore seeded above `MainScreen`
>    and renders first; `[b]` drops through, `[s]` returns. `run()` still exits
>    before rendering on `--auto` or a non-TTY, so no headless rip can block.
> 5. **`release_disambiguation` is written by `_emit_mb_provenance`**, not by
>    the menu. The menu's candidate list is empty on the single-match path —
>    the common case — so sourcing it there alone left the key absent on most
>    discs while the spec says absence means MB has no description. One
>    signal, two causes: the N4 shape, reintroduced and caught in review.
>
> **Side effect of N3 worth knowing about, not a defect:** the AcoustID picker
> now lists up to 43 releases per recording where it listed 25, so its
> `ResultsScreen` paginates to ~5 pages on the reference disc instead of ~3.
> That is the truncation being gone, not a regression.
>
> **Still open (deliberately not done):** after a manual pressing change,
> `acoustid_corroborates` is *disclosed* as having been computed against the
> previous release (`corroborated_release`) rather than recomputed. Recomputing
> is a pure set-membership retest with no network cost, but the per-track hits
> are not retained past R6. The §10.4 gate is unaffected either way — it matches
> at release-group level and every menu candidate shares the one plurality RG.
>
> **Noticed, not acted on: `match_distance` cannot see a manual confirmation.**
> It awards `mb_disc_id_multi = 0.30` (rather than `mb_disc_id = 0.50`) on the
> mere *presence* of `release_selected_via`, encoding "the pressing is a best
> guess". After N5 that is no longer always true: a `manual` selection is a user
> confirmation with the disc in hand, which is stronger evidence than a unique
> disc-ID hit, yet it scores the same 0.550 as the `auto_tiebreak` it replaced.
> The signal now exists (`release_selection`) and the scoring does not read it.
> Deliberately left alone — kgr asked for the menu, not a scoring change, and
> "the rest of the behaviour remains unchanged". Worth deciding separately.
>
> **Untested against hardware.** All seven containers on the shelf are the same
> album, so the menu path is covered only by fixtures built to the reference
> disc's measured shape. kgr's step 8 ("verify across several CDs") is
> outstanding, and the one thing fixtures cannot check is whether the annotation
> text is actually sufficient to pick a pressing with the disc in hand.


kgr's design, stated at the close of the 2026-08-05 investigation. Governs N1e,
N3, N4 and B-7 — read those for the measurements this rests on.

**The design.**
1. **Auto path unchanged.** The §10.3 cascade runs as today and the terminal
   `mbid` sort picks the winner. No behaviour change for `--auto`.
2. **No-auto opens the alternatives menu** — but *only* when the cascade reaches
   the terminal tiebreak with more than one candidate still tied.
3. **N1e gets a stay of execution.** Both AcoustID (once N3 is fixed) and Discogs
   stay in the pipeline.
4. The chosen release is the final disambiguation from whichever path ran.
5. **AcoustID and Discogs are reduced to a YES/NO existence check** on the
   *selected* release — not a corroboration verdict.
6. **Record MB's `disambiguation` free text** for the final candidate in PROV.
7. **Record whether the release was chosen automatically or manually**, so a
   future archivist can judge the provenance accuracy of the RBI.
8. Verify across several CDs before trusting it.

**Refinement, kgr 2026-08-05: the ladder splits by rung *kind*.** `preferred_country`
and the terminal `mbid` sort apply on the **auto path only**. The menu receives the
candidate set as it stands *before* those rungs run. The principle underneath it is
worth stating because it generalises: **a preference rung must never eliminate a
candidate from a menu a human is looking at.** Preference exists to break ties in
the absence of a human; with a human present it is exactly the wrong filter, because
it hides options on the basis of a config setting rather than evidence about the
disc.

| rung | kind | auto | no-auto (menu) |
|---|---|---|---|
| `_is_consistent` (MCN/ISRC) | evidence | ✓ | ✓ |
| R1 ISRC tally | evidence | ✓ | ✓ |
| plurality release-group | evidence | ✓ | ✓ |
| `barcode_plurality` | evidence | ✓ | ✓ |
| `preferred_country` | preference | ✓ | **✗** |
| `date` (earliest wins) | preference — **decided auto-only** | ✓ | **✗** |
| `mbid` (alphanumeric first) | arbitrary | ✓ | **✗** |

On this disc the menu would therefore show **7** candidates, not 5 — FR and AU
return to the list. That is *more* information, not noise: country and pressing date
are real distinguishing fields a user can check against the sleeve, and the AU row
is the only one carrying a date at all.

**`date` — DECIDED auto-only (kgr, 2026-08-05).** It sits *between* `preferred_country`
and `mbid` in the key tuple (`k_plur, k_country, k_date, k_mbid`), so naming the other
two auto-only had left it undecided. It is a preference: "earliest wins" is a rule
about which pressing to *prefer*, not evidence about which is in the drive — and it is
measurably destructive on sparse data. In the Discogs cascade it eliminated the ground
truth outright, because the truth carries no year and 19 of 24 candidates carry none
either. A rung that converts *missing data* into *elimination* must not run while a
human is choosing.

So all three of `preferred_country`, `date` and `mbid` are auto-only, and the menu
receives the set after `barcode_plurality` — the last evidence rung.

**Three things the design depends on, or needs sharpening.**

**(a) N4 is a prerequisite, not a neighbour.** "Ends with more than one candidate"
is not observable today. The cascade *always* ends with exactly one, because
`mbid` is terminal — the quantity the menu trigger needs is the **tie size at the
last discriminating rung**, which is precisely what N4 found is not recorded.
`release_selected_via` reports the first rung that *varies*, so on this disc it
says `preferred_country` while five candidates were still tied. Implement N4's
option (b) (`release_tied_after=<rung>:<n>`) first; the menu trigger is then
`n > 1` and the same key serves the archivist.

**(b) The auto/manual key wants three states, not two.** Two states cannot
separate the case where no choice existed from the case where an arbitrary one was
made, and those are opposite provenance claims:
  - `unique` — one candidate; nothing was chosen, so nothing can be wrong
  - `auto_tiebreak` — N candidates; the alphabetical `mbid` sort picked
  - `manual` — N candidates; the user picked, holding the disc
  - `rejected` — **added 2026-08-05**; the user was shown N candidates and said
    **none of these**. Required by the falsification recorded in N4: kgr's disc
    reads `FRANCE WE 835` and may not correspond to any of MB's seven. Without this
    state a menu forces a wrong endorsement, turning an honest guess into a false
    confirmation — worse provenance than never asking.
Only the middle state is a guess, and it is the one an archivist must be able to
find. Same shape as the CD-R offset-rescue finding (a gate needs three outcomes,
not two) and the `ctdb_declined` precedent. Note `_gate_adjusted_auto` can already
demote a run from auto to manual mid-disc, so the key must be written at the point
of selection, not inferred from the `--auto` flag.

**(c) What the YES/NO existence check actually means differs per source** — worth
writing down, because the two are not symmetric.
  - **Discogs**: "does the selected MB release carry a Discogs url-relation?" One
    call, no ambiguity, and 7/7 on this disc. Honest and cheap. This is *addressing*
    — it says a destination exists, nothing about whether the pressing is right.
  - **AcoustID**: "is the selected release among those MB says carry the
    fingerprinted recordings?" This is `acoustid_corroborates` re-framed honestly
    as membership. It is **weak but not vacuous**: the disc ID matched on TOC,
    whereas the recording→release linkage is a separate MB assertion, so a NO means
    the two disagree (wrong release pinned, or an MB tracklist error). Its **failure
    is more informative than its success**, which is why the existing
    `acoustid_gate` is already a fail-only key. Do not let the YES be read as
    pressing confirmation — AcoustID is edition-blind by construction.

**Fork SETTLED (kgr, 2026-08-05): nothing is deleted. "The rest of the behaviour
remains unchanged."** `_prepopulate_from_discogs` phase A *and* phase B, R11 and
§10.3.1 all stay exactly as they are; AcoustID needed no decision because it merges
nothing already. N1e's deletion list is **withdrawn in full** — this item is
additive only.

The measurements that motivated the deletion still stand and are worth keeping
visible, because they bound what the retained code can be expected to contribute:
Discogs supplied **0/18** values MB lacked on `catalog_number`/`label`/`country`,
and R11 differed on **0/8** albums. Neither is evidence of harm — the merge is
fill-blank, so it cannot overwrite a good value — so retaining it costs a network
call and buys, on the evidence to date, nothing measurable. That is a defensible
trade once the link has a real job; it is recorded here so nobody later reads the
retention as evidence the merge was validated. R11 is the one exception worth
watching: it is the only Discogs path that *overwrites* an already-set value
(`original_release_year`, when the Discogs master year is earlier), and the 0/8
sample never exercised the disagreement case it exists for.

**For reference, the retained automatic merge surface** (verified 2026-08-05):

| path | trigger | semantics | fields |
|---|---|---|---|
| Discogs phase A | always — **no Discogs call** | **overwrite** | `disc.barcode` ← canonical pick from MB hints |
| Discogs phase B | exactly 1 search result **and** `_albums_match` | **fill-blank** (disc wins) | album, artist, barcode, disc_number/total, release_date, catalog_number, label, country, original_release_date, MB ids, discogs_release_id, set_title, + per-track title/performer/ISRC |
| Discogs R11 | `discogs_release_id` set | **overwrite if earlier** | `original_release_year` |
| Discogs §10.3.1 | selected release | PROV only | — |
| **AcoustID (all paths)** | — | **merges nothing** | only `acoustid_corroborates` + the gate |

**Testing.** The link-existence rate is already measurable without discs (19/24 =
79% general; 7/7 on this disc's candidate set). The full auto-vs-menu path needs
real containers from **different** discs — all seven on hand are the same album, so
they cannot exercise a differing tie size or a blank `disambiguation`.

#### N2. **[C2I] Build the Q + C2 map, and restore the per-track marker on the
progress bar**

> **PLAN: `docs/reference/progress-map-plan.md` (2026-08-07).** Bench built
> (`tools/progress_lab.py`); aesthetic settled by kgr — `glyph` / `cb` / `ramp`,
> no ruler. **The headline finding is that almost nothing needs building**:
> `Device.read(status_map=True)` already supplies the frontier (`PENDING`), the
> C2 lane and a per-sector severity nibble, and already composes with `sink` /
> `c2` / `sub`; `read_span(**kwargs)` already forwards it. Two requests were
> withdrawn *before* being sent because they asked for shipped features. The one
> genuine gap is the **Q lane** — `ReadStats.subq_ok` proves AccuDisc runs the
> per-sector CRC and discards only the position — asked as a parallel `subq_map`
> in outbound §148, along with the question that can invalidate the design if
> answered late: **does Q lag the audio, as C2 does?** ~~The per-track marker is
> the left-hand status text and has been there all along~~; the ruler row was an
> addition, and kgr dropped it.
>
> **Amended 2026-08-12 — "has been there all along" is wrong.** The track number
> was *shipped with the Q lane* (`bd1b5eb`), read from the TOC
> `_read_disc_binding` already fetches, so it costs no extra command and no
> second spin-up. `progress-map-plan.md` §6.4 is the authority and says so.
>
> **C2 lane + frontier SHIPPED 2026-08-08.** `disc_map.py`, `TerminalUI.set_map`,
> `accudisc_reader._census_c2` / `read_disc_c2(map_cb=…)`, 33 new tests. Q lag
> closed on 42 captures (all NO LAG). **The headline finding above needs one
> correction**: through the Python binding the status map is unreachable until
> the read *returns* (`Device.read` allocates it internally and surfaces it only
> on `ReadResult`), so the C2 lane is computed in the sink instead — no loss,
> since the states the sink cannot see need reread machinery this path does not
> use. The C API already takes a caller-supplied buffer, so the amended ask is a
> **binding** change that serves `status_map` and `subq_map` alike. ~~Remaining:
> the Q lane (blocked on that ask — never DIY, see the zero-fill trap), the
> recovery-ladder rendering, and the static post-CTDB map.~~
>
> **Amended 2026-08-12 — the Q lane is SHIPPED and the ask was answered.**
> AccuDisc 0.5.0 delivered both capabilities; measured today the binding
> publishes `features = {caller_map_buffers, speed_bands, speed_honoured,
> subq_map}` against library 0.9.0. Landed as `bd1b5eb` (Q lane + track number)
> and `cbd1b7a` (Q-lane calibration — Q's healthy baseline is a few per cent,
> not zero, so the two lanes carry different severity bands). `NO_POSITION`
> counts as healthy (~1% interleave would otherwise flag every cell on a clean
> disc); `NO_AUDIO` counts as damage, because the lane answers "is the
> subchannel intact here". A binding without the capability draws one lane and
> says so — it never draws Q as healthy.
>
> **Genuinely remaining — `progress-map-plan.md` §6, steps 5 and 6 only:**
> 1. **Recovery-ladder rendering** (§2) — our own work, nothing blocks it.
> 2. **A static map after CTDB repair**, via the pre/post diff (§5) — explicitly
>    a sketch, not a promise.
>
> Do not re-derive this list from here: §6 of the plan is the authority and it
> is current. This entry is a copy and drifted once already.

Two halves of one design. The track-number status to the left of the progress bar
was lost in the AccuDisc switch and never reimplemented. And per AccuDisc's preview,
**the progress bar will *be* the Q and C2 map** — so this is not a bar with a map
bolted on, it is one widget that has to be designed and tested as such.

kgr's suggestion, and it is the right shape: **build a standalone tool that is just
the progress bar**, for development. Rationale beyond convenience — a TUI element
driven only by a live rip can be exercised once per disc, at 12 minutes a run, on
whatever error pattern the disc happens to have. A tool that replays a captured or
synthetic stream can be iterated in seconds and can present error patterns on
demand. This is the `feedback_validate_timing_dependent_ui` rule applied before the
fact rather than after: prefer a timing-independent design validated by instant
replay, and capture the real stream *before* writing the renderer.

Inputs already available: `read_disc_c2`'s chunk callback (the existing progress
source), the C2 bitmap, and the raw P-W subchannel — `subchannel.scan_subcode`
already decodes Q per sector with CRC validation, so "Q good / Q bad" is a per-sector
signal we can already produce. AccuDisc's `--map-file` mmap status map was noted as
"not yet consumed" back in July and is the other candidate input; check what the
binding exposes now before designing around the CLI-era artefact.

#### N6. **[C2I] `match_distance` is computed before the menu and cannot see a
confirmation** — QUEUED 2026-08-07, kgr thinking

Raised as a footnote to N5 ("`match_distance` cannot see a manual confirmation"),
investigated 2026-08-07, and the footnote understated it. **The ordering is the
defect, not the weights.** `build_match_distance` runs at `cdda2img.py:2137` (and
`:803` on the create path); `run_metadata_menu` runs at `:2165` (`:815`). The score
is never recomputed. So `match_confidence` is a **pre-menu** number stored in the
same PROV dict as `release_selection`, which `_record_pressing_outcome` writes
*after* the menu closes — two keys describing two different moments, with nothing
recording which is which. Every N5 container carries `match_confidence=0.550`
beside `release_selection=manual`, computed before the manual choice existed.

~~**The prior question is kgr's**~~: is `match_confidence` a pre-menu *hint* (its
2026-06-20 role — informational, never skips the menu) or a record of what the
container ended up believing? All three fix options below are unreachable at line
2137 regardless of which is chosen, so this is not a scoring-table question yet.

> **ANSWERED by kgr 2026-08-12 — and it answers a third way, not one of the two.**
>
> 1. **`match_distance` runs *after* the menu** on the non-auto path. The auto
>    path is unchanged (no menu ran, so there is nothing to wait for).
> 2. **`release_selection=manual` ⇒ `match_confidence=1.000`.**
>
> kgr's reasoning, which is what settles the framing: *the purpose of
> `match_confidence` is to say how confident the automatic **guess** is, and a
> manual selection is not a guess — it is a certainty.* So the key keeps its
> 2026-06-20 meaning (it scores the guess) while being **recorded at the moment
> that meaning is finally determined**. The old defect was never that the number
> described the wrong thing; it was that it was written before the guess could be
> superseded, leaving `match_confidence=0.550` beside `release_selection=manual`
> in every N5 container — two keys describing two moments with nothing saying so.
>
> **This is not option (1), (2) or (3).** Option (1) bumped `manual` to a rung
> above 0.50 inside the MB branch; kgr's rule short-circuits the scorer entirely
> at 1.000, so the "how MB found it" axis never runs on a manual pick and option
> (1)'s weakness (a user-confirmed duration match still reading 0.20) cannot
> arise. It is closest to option (2) in *shape* — confirmation is its own axis —
> but it is a **replacement** rather than an additive contributor.
>
> **Two consequences to handle when implementing, both already recorded below.**
> - Moving the score after the menu **brings `mb_duration_match` (+0.20) to
>   life.** It is unreachable today because stage-7 routes through
>   `strip_pressing_mbid`, nulling the id before a pre-menu scorer could see it.
>   Under the new ordering the branch can fire, so it needs to be *correct*, not
>   merely present — it has never executed on real data.
> - `rejected` still has no home. kgr's rule covers `manual` and leaves
>   `auto_tiebreak` at today's 0.30, but `release_selection=rejected` is negative
>   evidence about the candidate *list* and nothing reads it. Not a blocker —
>   flag it rather than invent a value.
>
> **Cheap, because nothing reads the score back.** `match_confidence` /
> `match_recommendation` are write-only tree-wide; the one live consumer is the
> printed `match_dist.summary()` line, which is pre-menu **by design** and earns
> its place there. So the printed line stays where it is and only the *stored*
> key moves — they stop being the same number, which is the point.
>
> **Status: unblocked, not implemented.** No code queued as of 2026-08-12.

**Nothing reads the score back.** Tree-wide, `match_confidence` /
`match_recommendation` are write-only: two writes in `cdda2img.py`, a man-page
entry, a flow doc. No consumer in `catalogue`, `list` or `test`. The one live
consumer is the printed `match_dist.summary()` line, which is pre-menu by design
and earns its place there. Recomputing after the menu (or storing both) therefore
costs nothing but the decision about what the stored key means.

**Options** (none chosen):
(1) **Read `release_selection` in the existing MB branch** — `manual` → above the
0.50 unique-hit rung, `auto_tiebreak` → 0.30 unchanged, `rejected` → its own
value. ~6 lines, no new PROV. Weakness: folds confirmation into the
"how MB found it" axis, so a user-confirmed duration match still reads 0.20.
(2) **A separate additive `user_confirmed` contributor** — keeps "how it was
found" and "did a human check it" as the independent axes they are, and is the
only option where `rejected` (negative evidence about the candidate *list*) has an
honest home. Nothing reads `rejected` today.
(3) **Do it inside B-7** — feed the resolver's `Resolution` in and delete the PROV
string-sniffing. Right destination, wrong next step; see below.

**Why (3) does not work yet — two structural findings.**
- **The Trust ladder has no rung for an arbitrated pressing.**
  `resolver_adapter.py:381` emits `Field.MB_RELEASE_ID` with one `src` for the
  whole disc-field block, so a disc-ID match proposes `Source.MB_DISC_ID` @
  `Trust.DISC_ID` **whether MB returned one release or seven**. The §10.3 ladder is
  *intra*-source; the resolver is *cross*-source. Option 3 as stated would
  therefore **lose** today's 0.50-vs-0.30 distinction, not replace it — making it
  work means adding a trust rung, a larger design act than the tweak it avoids.
- **There is no `Resolution` to read.** B-5 re-resolves `ctl.disc` on every apply
  and the persistent menu accumulator does not exist (B-7, above). Option 3 is
  "build the accumulator first" — which is the *same* prerequisite a post-menu
  score of any kind needs, so the dependency is shared, not wasted.

**The manual signal is already structured, though** — `menu_state.py:1175` applies
the N5 pressing choice via `apply_menu_selection(..., overwrite=True)`, which
`resolver_adapter.py:567-569` emits as `Source.MENU` @ `Trust.MANUAL`. Options 1
and 2 would re-derive from PROV strings something the resolver already knows.

**Two seams found on the way, filed here rather than fixed:**
- **`mb_duration_match` (+0.20) appears unreachable.** It needs `disc.mb_release_id`
  set *and* `duration_match_release` in PROV, but stage-7 routes through
  `strip_pressing_mbid` (`mb_lookup.py:928`), which nulls the id precisely so a
  recording-level source cannot assert a pressing (C2). The guard cannot be true
  from that path, and the score runs before the menu could set the id another way.
  Moving the score after the menu would bring the branch to life.
- `Source.DURATION` is in `_RECORDING_LEVEL` and `FieldProposal.__post_init__`
  raises on it proposing `MB_RELEASE_ID` — the scorer describes a state the
  resolver forbids. More evidence the two models have drifted apart.

#### N7. **[C2I/K] Is the 120-byte tail shortfall purposeful?** — **ANSWERED
2026-08-11.** It was not a shortfall at all: the assumed audio origin was 120 bytes
too high. kgr supplied three further PlexTools images the same day.

**Result.** `_AUDIO_OFFSET` was `0x6007B`; the true raw-audio origin is **`0x60003`**.
From there every image is *exactly* a whole number of sectors. The 120 bytes are
present at the **head**, not missing from the tail — they are the first 30 samples of
LBA 0, which the drive's +30 read offset discards. kgr's call ("not a bug, it has a
purpose") was right, and the purpose is simply that the file is a raw read.

**PXI stores RAW audio.** Settled by AccurateRip, which is offset-sensitive by
construction: fed the audio from the measured origin, **+30 verifies and 0 does not**.

| image | +30 | 0 |
|---|---|---|
| disc A, 11 tr | 11/11 conf 4400 | 0/11 conf 0 |
| disc A, re-rip | 11/11 conf 4400 | 0/11 conf 0 |
| disc B (ABBA), 19 tr | 18/19 | no match |
| disc C, 12 tr | 11/12 | no match |

This refutes "already corrected" **without knowing the drive** — correction is
correction, so a corrected stream verifies at 0 whatever wrote it. Every other
verifying offset (-639, -1967, +1573 ...) differs per disc: pressing cohorts, exactly
as `accuraterip.detect_offset` warns. **+30 is the only offset common to all four**,
which is the signature of a drive rather than a disc. Note part 2 of the original plan
(a drive with a different offset) proved *unnecessary* for this question — AR answered
it from one drive, because it compares against the world's rips rather than ours.

**Why the original evidence was under-determined, not wrong.** The unique byte-match
that pinned `0x6007B` was against our own *offset-corrected* rip, so it necessarily
landed at origin+120. Both readings did predict identical bytes, as recorded; what
broke the tie was an instrument sensitive to the quantity in dispute.

**The other four questions, also answered:**

- **Are `_TOC_OFFSET` / `_INDEX_TABLE_OFFSET` / `_AUDIO_OFFSET` fixed? YES.** CD-Text
  ran 0, 758 and 848 bytes across the four images — including a disc with none at all,
  kgr's "cheapest test there is" — and none of the three offsets moved.
- **Is there ever an MCN? YES.** Two discs carry one (`5099746863722`,
  `7559607740206`), both valid GTIN-13, read correctly from `0x804C`; the all-zero
  sentinel is confirmed by the two that do not.
- **Does anything occupy `0x305-0x8000`? NO — the question dissolved.** That boundary
  was one disc's CD-Text *end*: `0x0B + 2 + 758 = 0x305` exactly. A disc with more
  CD-Text writes further (Tracy Chapman, 848). No structure, no ISRC table; ~~ISRCs
  parse as absent on every image.~~ `0x85BF-0x60003` remains zero throughout.

  > **CORRECTED 2026-08-12 — "ISRCs parse as absent on every image" is wrong.**
  > The scoped claim (nothing in `0x305-0x8000`) stands; the general clause does
  > not. **The PXI carries per-track ISRCs**, found while searching the header for
  > a drive-offset field. On the 12-track disc, twelve values pass
  > `validators.validate_isrc`, at `0x805B + n * 0x48`, alongside its MCN at
  > `0x804C`:
  >
  > ```
  > 0x0805B  BED018901375     0x080A3  BED019001477
  > 0x080EB  BED018901377     ...      0x08373  BED018901386
  > ```
  >
  > Zero-filled on the other three, consistent with those discs carrying none.
  > **`pxi_reader` does not read them** — a real metadata loss on discs that have
  > them, since ISRC is one of the few objective identifiers we trust.
  >
  > **Deliberately NOT parsed yet, for the format's own documented reason.**
  > `0x48` is `2 x 36`, so the stride assumes **two index records per track** —
  > precisely the assumption this format punishes. Every image we hold has exactly
  > two, so "72-byte per-track stride" and "12 bytes before every *even* index
  > record" fit the data identically and predict different things on a disc with
  > INDEX >= 02. Resolving it needs the same disc the last bullet is waiting for,
  > which makes these one item and not two. Filing a guess as a parser would bake
  > the coincidence in.
- **More than two index records per track? STILL UNTESTED.** No image has an
  INDEX >= 02, so `index_points` has never executed on real data.

**What remains open, and it is a different question.** The reader now applies
`_PLEXTOOLS_READ_OFFSET = 30` explicitly and records `pxi_read_offset` in PROV. Since
the audio is raw and the file records no drive identity, **a `.pxi` written by a drive
with a different read offset imports shifted by the difference.** That is now a stated
limitation rather than a silent accident, but it is unresolved policy: hardcode,
prompt, take a CLI flag, or store raw and correct downstream. kgr's call. A rip on a
non-+30 drive would confirm the mechanism, and is the one part of the original part 2
still worth doing.

> ~~**DECIDED 2026-08-12 — leave the importer as-is (kgr).**~~ **SUPERSEDED the
> same day: kgr called for AccurateRip detection per import. SHIPPED `72c2fa1`.**
> The offset is now measured, and the interesting part is not the detection but
> the **choice among its answers**, which had to be measured too:
>
> ```
>   disc A (12 tr)    +1573, +30, -634            <- +30 ranks SECOND
>   disc B (19 tr)    +30, -1710, -645, -1979, +2482
>   disc C/D (11 tr)  +30 and twelve others, TEN of them inside +/-1500
> ```
>
> So `matches[0]` installs a pressing cohort as a drive offset on disc A, and a
> plausibility band is no better — a cohort is free to land next to zero. Neither
> rank nor magnitude separates the two. The resolver therefore treats the **prior
> as evidence** (it is the N7 cross-image discriminator, already evaluated) and
> lets AccurateRip confirm or contradict it; five outcomes in
> `pxi_offset_source`: `accuraterip_confirmed`, `accuraterip_sole`,
> `assumed_ambiguous` (declines rather than guesses), `assumed`,
> `assumed_unverified`. Only **confirmed** matches count — ABBA *Gold* has an
> offset where the frame-450 prefilter hits 19/19 and no full-track checksum
> agrees.
>
> **CTDB was deliberately not wired.** It inherits the same pressing cohorts (the
> −669 twin appears in both databases), so it cannot disambiguate what AR cannot,
> and it would add a second network dependency plus the image-domain conversion
> this project has already got wrong in both directions.
>
> Acceptance: all four images import **byte-identical** to the previous code, all
> resolving `accuraterip_confirmed` at `+30`. That test caught a real bug the unit
> tests structurally could not — they inject a fake supplier, so the live one had
> never executed.
>
> **Still open, unchanged:** no `.pxi` from a second drive exists, so
> `accuraterip_sole` — the branch that makes this feature worth having — has never
> fired on real data.
>
> **A proposal was tested and refuted on the way — record it so it is not
> retried.** The idea: *"the PCM truncation size effectively IS the offset"*,
> reusing the reverse-engineering step. It cannot work, for two reasons that are
> worth separating.
>
> 1. **There is no truncation.** Measured across all four images:
>    `size - lead_out * 2352 = 0x60003` **exactly**, whole sectors on every one.
>    Residue is identically zero.
> 2. **The 120 bytes were circular.** They existed only against the *assumed*
>    origin `0x6007B`, which was itself found by byte-matching our own
>    **+30-corrected** rip — so the shortfall was `offset * 4` *by construction*.
>    Fed back as a detector it returns the number that produced it.
>
> The structural reason behind both: **a raw dump of N sectors is `N * 2352`
> bytes whatever the drive's read window is.** The offset changes *which* samples
> land in those bytes, never how many. So no size-derived test can recover it —
> and a `.pxi` from a `+667` drive would report `0` and silently break the
> AR-verified path. This is the same shape as the original N7 error one level up:
> a quantity derived from an assumption cannot then test that assumption.
>
> **"The offset is probably stored in the file" — searched, not found.** One drive
> wrote all four images, so a stored drive field must be **byte-identical across
> all four**; that filter cuts the search to 81 non-zero constant bytes in the
> 393,219-byte header. Looking for `30 / -30 / 120 / -120 / 60 / 12` at every
> constant position in ten integer encodings (u8, i8, u16, i16, u32, i32; LE and
> BE) returns **0 hits**. The non-zero constants are the `PXI` magic, `11 03` at
> `0x04`, `11 11` at `0x07` / `0x801F` / `0x8023`, and 24 bytes of `0x99` at
> `0x802F` before the track-count byte. None is offset-shaped. There is also **no
> drive identity string** anywhere — the only ASCII in the header is CD-Text, the
> MCN and the ISRCs above — so the AccurateRip drive database cannot be consulted
> either.
>
> **What that does and does not establish.** It is a genuine negative, not
> silence: the probe would have fired had the value been present in those forms.
> It does **not** exclude BCD, a scaled representation, or an index into a
> PlexTools-internal drive table. And the ceiling is structural — with one drive,
> a constant field is indistinguishable from any other constant, so further
> searching is worth less than one image from a second drive. That single artefact
> would resolve this, the ISRC stride, and the INDEX >= 02 question together.
>
> **The writer agrees, and explains why (kgr's idea, 2026-08-12).** Strings from
> `PTPXL.exe` (PlexTools Professional XL 3.x, 2007) put the offset in a
> **`CDriveInfo`** block — between `Current Firmware: %s`, `Access Time:` and
> `Interface: %s`, ending at `DriveInfo.txt`:
>
> ```
>   0x4629c8  -%d bytes            <- the %s below
>   0x4629d4  %d bytes
>   0x462830  CDriveInfo
>   0x462a80  Audio Write Offset: %s
>   0x462a98  Audio Read Offset : %s
>   0x462ab0  DriveInfo.txt
> ```
>
> So PlexTools models the offset as a **property of the drive**, listed with
> firmware and interface and exported to a drive report — not as a property of
> the image. Two things follow.
>
> - **It expresses offsets in BYTES**, not samples. The PX-716A's `+30 samples`
>   is `120 bytes` in PlexTools' own units — the exact number this whole item
>   chased. `120` was in the search set above and returned nothing.
> - **`Due to the offsets the last track can be different`** (`0x45fbe4`) sits in
>   the *Disc Copy* log beside `Verifying Track %d` and
>   `include: UPC: %s ISRC: %s CD+G: %s`. Plextor documented our tail-pad effect
>   twenty years ago, as a read-vs-write offset consequence on the last track.
>   (That same string is independent corroboration for the ISRC field above —
>   PlexTools does capture UPC and ISRC.)
>
> **Strings show what is displayed, not what is serialised**, so this is not proof
> — a binary header field need not have a string near it. The header search is the
> direct evidence; this supplies the missing *why*, and the two are independent.
> Working conclusion: **the offset is not in the file because PlexTools never
> considered it part of one.** It reads it from the drive at runtime (Plextor
> drives report it over a vendor command), which is exactly the information a
> `.pxi` carried to another machine loses.

**Instrument:** `tools/pxi_probe.py` (origin arithmetic, header-fill boundary,
`--ar` offset discrimination). Images at
`/mnt/aladdin/Storage/Install/Burning/Images/Plextools/`.

#### N8. **[C2I] Any `log` record emitted while the TUI is live orphans a progress
bar** — QUEUED 2026-08-10, found while fixing the import readers

**Mechanism, measured under a pty 2026-08-10.** `TerminalUI._frame` repaints by
rewinding `_prev_height - 1` lines and erasing to the screen bottom, where
`_prev_height` is the TUI's model of its own region. Any write it did not make
moves the cursor without updating that model, so the next rewind lands in the
wrong place and the previous frame is never erased. **One stranded progress bar
per stray write** — kgr saw three during a `.pxi` import: two orphans plus the
live one that `stop()` clears.

The reader half is fixed (readers take a `report` sink; `_import_source` binds it
to `_ui_print`). **The logging half is not, and it is the larger surface.**
`main()` installs a handler only under `--verbose`, so at default verbosity a
`log.warning` falls through to `logging.lastResort` → stderr, with the same
effect and from any module. `accudisc_reader` and `subq_toc` alone hold 7
`log.warning`/`log.info` calls, several on the rip path
(`_log_read_caveats`, `_log_speed_adopted`, the CD-Text binding guard). It has
probably not been reported because those fire only on caveats, quantised speeds
and rejected CD-Text — i.e. exactly when the operator most wants a clean screen.

**The obvious fix has a rule in front of it.** A `logging.Handler` that calls
`ui.add_output`, installed in `TerminalUI.start()` and removed in `stop()`, is the
natural answer — but CLAUDE.md forbids global logging mutation inside `src/`
("install narrow filters at `__main__` entry only"), and it would double up with
`basicConfig`'s own `StreamHandler` under `--verbose`. Worth doing properly:
decide whether the handler belongs at the entry point (installed when a TUI is
created), how it interacts with `--verbose`, and what level it filters at.

**Do not "fix" this by removing the log calls.** They are the right records; the
defect is that there is no route from a log record to the TUI output region.

**Reproduction harness** (write it fresh, it is ten lines): drive a real
`TerminalUI` under `pty.spawn`, emit a `log.warning` mid-render, and look for a
`\r\x1b[J` rewind where a `\x1b[<n>A\r\x1b[J` was needed. Include the negative
control — the old code path — or the probe cannot fail and proves nothing.

### ⭐ LIVE — outstanding work as of 2026-07-30

Consolidated from both projects. AccuDisc is a peer project with its own
`docs/reference/TODO.md`; items owned by them are listed here only where our work
depends on them. Owner: **C2I** = cdda2img, **AD** = AccuDisc, **K** = Keith.

#### Blocked on AccuDisc's `make install` reaching this machine

AccuDisc pushed `make install` + a wheel + `accudisc 0.4.0` on 2026-07-29. Nothing
below can start until the library and binding are actually installed here, because
every step assumes the binding resolves without our `tools/` shim.

> **UNBLOCKED 2026-08-12 — kgr ran AccuDisc's installer (artefacts dated 17:11).**
> Measured, not assumed: `/usr/local/lib64/libaccudisc.so.0 -> .so.0.9.0`; the
> wheel directory holds `accudisc-0.9.0-cp310-abi3-linux_x86_64.whl` **and only
> that** (0.4.0 and 0.5.0 are gone, so `install.sh`'s version-sort now has one
> candidate); the pipx venv carries binding 0.9.0 resolving to the *installed*
> library; `doctor` reports it `ok` with no shim warning. The gate at the head of
> this block is therefore lifted — but read each item below, because two of them
> were **not** waiting on the install and are still open.

1. ~~**[C2I] Install and verify.**~~ — **DONE 2026-08-12.** `cmake --build build
   --target wheel` produces a `cp310-abi3-linux_x86_64` wheel; install it
   alongside their `make install`. The wheel's RUNPATH is the configured install
   prefix, so wheel and install belong together — installing one against a
   different prefix reproduces the failure the staging split exists to prevent.
   Verified end to end: `cdda2img doctor` names
   `~/.local/pipx/venvs/cdda2img/.../accudisc` 0.9.0 → `/usr/local/lib64/libaccudisc.so.0`,
   i.e. binding and library from the same install rather than the build tree.
2. **[C2I] Set `ACCUDISC_REQUIRE_INSTALLED=1` in whatever installs the binding.**
   Their discovery order is `ACCUDISC_INCLUDE_DIR`+`ACCUDISC_LIB_DIR` → `pkg-config`
   → their checkout. `pkg-config` wins **when it answers**, and on a machine that has
   never installed it is silent, so the checkout fallback wins by default. That is how
   our 2026-07-29 pipx probe ended up linked to their build tree while reporting
   success. The env var turns that fallback into a build-time error.
   **Amended 2026-07-30 (§125/§cn/§co).** "Silent on a machine that has never
   installed" understates it: `pkg-config --exists accudisc` returns 1 on *this*
   box, where the install did happen — the prefix is `/usr/local` and pkg-config's
   built-in `pc_path` excludes `/usr/local/lib64/pkgconfig`. So a build here today
   still takes the checkout branch, install or no install, and `ACCUDISC_REQUIRE_INSTALLED=1`
   would currently turn that into a hard failure with nothing to fall back to.
   Sequencing consequence: **do not set the env var until the search path is
   supplied.**
   **Amended again 2026-08-02 (§l): the discovery rung this used to wait on is
   withdrawn and will not be built.** The plan was `accudisc config` reporting
   `pkgconfigdir`, us setting `PKG_CONFIG_PATH` from it, `pkg-config --exists`
   validating — the binary supplying a search hint, the `.pc` supplying the
   answer. Three things killed it, and the third is worth keeping after the
   feature is gone. (a) Since our API migration the CLI is a development front
   end, so bootstrapping the preferred interface by shelling out to the
   deprecated one is backwards; it is also `option(ACCUDISC_BUILD_CLI ON)`, and a
   discovery channel a supported build can omit is not a channel. (b) The gap it
   filled is filled: `accudisc.pc.in` now carries `wheeldir`, which was the one
   thing the `.pc` could not answer when §co was written. (c) **Its premise was
   false at exactly the prefix that motivated it.** §126.2 rested on "`accudisc`
   is on `$PATH`, and that is true at every prefix" — but `/usr/local/bin` on
   `$PATH` while `/usr/local/lib64/pkgconfig` is off `pc_path` is an asymmetry
   *unique to `/usr/local`*. At `/opt` or `~/.local` neither is on a default
   search path, so the CLI would be exactly as unfindable as the `.pc` and the
   rung buys nothing. It was never a general answer to "installed somewhere, find
   it"; it was a workaround for one distro convention.
   What survives is the finding, not the feature: **a binary cannot distinguish
   "paths I will be installed to" from "paths I was installed to"** —
   `build/cli/accudisc` and `/usr/local/bin/accudisc` come from the same
   CMakeCache and would emit byte-identical output, and only one corresponds to an
   install. That applies to any future self-description feature either side
   proposes. So this item now needs `PKG_CONFIG_PATH` set explicitly (or the
   `.pc` installed somewhere `pc_path` covers) before the env var goes on;
   there is nothing left to wait for.
3. **[C2I] Retire the shim.** Delete `tools/accudisc/accudisc` and
   `tools/accudisc/pybinding`, delete `_binding_search_path()`, and declare `accudisc`
   as a real dependency. `cffi` then arrives as *its* dependency and the stopgap line
   in our dev group goes.
   ~~**The completion signal is now visible** (`80384ba`, prompted by §co.4): `doctor`
   reports the binary that will actually run, the `libaccudisc` it links, and any
   `$PATH` install it shadows. Today that reads `tools/accudisc/accudisc` →
   `build/src/libaccudisc.so.0`, *shadowing* `/usr/local/bin/accudisc` — two different
   artefacts (`991cb02` vs `cf1a248`, different `RUNPATH`s). This item is done when
   that line names the install and the shadow line is gone.~~

   **Amended 2026-08-12 — that completion signal no longer exists, so it was
   replaced rather than evaluated.** Since the CLI retirement `doctor` deliberately
   does **not** report the `accudisc` binary at all (CLAUDE.md: a line saying `ok`
   about an artefact this application never executes is the defect the binary line
   was added to fix, inverted). Confirmed in today's output — the AccuDisc group
   shows `cffi` and `accudisc (binding)` with a `libaccudisc ->` sub-line, and no
   binary or shadow line. "The shadow line is gone" is now **unfalsifiable**: it is
   satisfied by the check having been deleted. A signal that cannot fail is not a
   signal.

   **New completion signal, reachable and failing today:**
   `uv run python -c "import accudisc"` succeeds in the dev checkout *without*
   `_binding_search_path()` appending the shim. Measured 2026-08-12: it raises
   `ModuleNotFoundError`, so the shim is still load-bearing for the checkout even
   though the pipx install is clean. **Status: the install half is done, the
   checkout half is not.** Closing it means installing the binding into the dev
   venv (or declaring `accudisc` a real dependency, which is the item's own plan)
   before the two symlinks and `_binding_search_path()` can go.
4. **[C2I] Packaging.** `pipx install .` + `pipx inject cdda2img <accudisc binding>`
   is proven end-to-end (binding active, `transport: binding`, real archive rendered).
   A `make install` target should wrap those two commands rather than hand-rolling a
   site-packages installer. **Do not** mix distro packages with the pipx venv: five
   deps are PyPI-only regardless (`av`, `blake3`, `ortools`, `pyebur128`,
   `questionary`), so the mixed model buys fragility and no coverage. Full audit in
   `private/DEPS.md`.
   **The split install has a cost `tools/disc_ab.py` must absorb first (AccuDisc
   §cp.1/§cp.4, 2026-07-30).** Under `pipx install` + `pipx inject`, the injected
   wheel's RUNPATH is the *install* prefix (`/usr/local/lib64`), while the
   `tools/accudisc/` symlink keeps the CLI on AccuDisc's build tree. Two routes,
   two `libaccudisc.so.0`s. That is fine for running and fatal for measuring: the
   A/B's confound-freedom rests on both carriers resolving **one** library
   (CLAUDE.md states this), so the instrument would silently start comparing two
   library builds while still looking like a carrier comparison — the
   `binding_ab.py` error again, with a packaging decision as the trigger instead
   of a default flip. Today there is no skew (both resolve `build/src/`, measured);
   `doctor` reports both libraries and flags divergence as of `9012639`, so the
   condition is at least visible. **Before adopting the split: pin the A/B's
   library explicitly, or re-inject with `pipx inject --force` as part of the
   measurement protocol.**
   **Keith's ruling, 2026-07-30: the installer checks for missing dependencies and
   does not install them.** `cdda2img doctor` is that check and it already exists
   (below); what remains here is the `make install` wrapper itself, which must end by
   invoking `doctor` and must not acquire an install-things mode later.
   **DONE 2026-07-31 — as `install.sh`, not a `make install` target**, because it has
   to run from an unpacked source tarball where `make` may not be installed and the
   user is not a developer. Four steps + `doctor`, honouring the ruling. AccuDisc's
   wheel is **found, not asked for**: their `install.sh` header and `cmake/accudisc.pc.in`
   already froze the contract as `$PREFIX/share/accudisc/wheel/accudisc-*.whl`, *the
   directory, not the filename*. Three search routes; pkg-config is tested for a
   non-empty answer rather than exit 0 — measured, `--variable=wheeldir` returns rc=0
   with empty stdout against the installed 0.4.0 `.pc`, so branching on rc yields `""`
   from the success path. Reinstall is uninstall-then-install: `pipx install --force`
   over a venv from an earlier pipx session fails (uv refuses to overwrite, pipx
   declines to remove, exit 1). Sent to AccuDisc as §130; the two open questions there
   (is the layout frozen; `ACCUDISC_PC_WHEELDIR` appears never `set()`) do not block us
   because the glob needs no pkg-config.
   ~~**Still open here:** the `disc_ab.py` precondition above, and AccuDisc's §cq.2
   remedy for it — build the A/B wheel with `-DACCUDISC_INSTALL_RPATH` pointed at the
   build tree, in a **separate throwaway tree**, and never let that wheel leave the
   machine or become the one Keith installs.~~

   **Amended 2026-08-12 — MOOT. Both A/B instruments were deleted 2026-08-01.**
   `tools/disc_ab.py` and `tools/binding_ab.py` went with the CLI retirement, so
   there is no two-carrier measurement left for a library skew to invalidate and
   nothing to pin. Item 4 has no open work.

   **The finding survives the instrument and is the reason to keep this block.**
   Under `pipx install` + `pipx inject`, the injected wheel's RUNPATH is the
   *install* prefix while a `tools/` symlink keeps its target on the build tree —
   two `libaccudisc.so.0`s, fine for running and fatal for measuring. That
   generalises to any future instrument comparing two paths into AccuDisc, and it
   is the `binding_ab.py` error a second time with a packaging decision as the
   trigger instead of a default flip (`feedback_control_relative_to_environment`:
   re-ask "can this instrument still fail?" whenever a default or a path moves).
4a. **[C2] `doctor` should keep growing with the package's data files.** The
   **Package data** group added 2026-07-31 checks the recovery profiles only. It exists
   because `doctor` reported *21 ok, 0 warnings* on an install where `rip` aborted at
   startup: the profiles lived in a top-level `conf/` the wheel never packaged, and a
   dependency checker had no reason to notice. Any future non-`.py` file the package
   must carry needs a line in that group, or the same hole reopens with a different
   file. Related sweep worth doing once: `Path(__file__).parent.parent` in `src/` is the
   tell for this defect class — resolution that works in a checkout and nowhere else.
5. **[K/AD] `setcap` the installed binary.** Enabled in their install
   (`ACCUDISC_SETCAP_ON_INSTALL`) but not in effect until the binaries actually
   invoked come from the install. Until then their rebuild drops the capability from
   the inode *we* execute — a **measurement-validity** defect, not an inconvenience: a
   rip with the vendor path disarmed is a different configuration, so any bench run
   straddling a rebuild compares two configurations while looking like one series.

   **Amended 2026-08-12 — half done, and the remaining half rides on item 3, not
   on AccuDisc.** The install half is satisfied: `getcap /usr/local/bin/accudisc`
   reports `cap_sys_rawio=ep` (a real file, not a symlink — `getcap` on a symlink
   prints nothing and exits 0, which would have read as a negative). But the
   binary *actually invoked* is still the build-tree one, because
   `tools/recovery_bench.py` is the last CLI consumer and reaches it through the
   shim. That inode carries the capability **today**
   (`~/Git/accudisc/build/cli/accudisc`, `cap_sys_rawio=ep`) — which is exactly
   the fragile state this item describes rather than the resolution of it, since
   the next rebuild drops it silently and a bench run straddling that rebuild
   still compares two configurations. **Do not close this until item 3 closes**
   or `recovery_bench` is pointed at the install.

#### Then, and only then

6. ~~**[C2I] Retire the subprocess transport entirely**~~ — **DONE 2026-08-01**
   (`f8f1b38`, "retire the CLI — the API is the only transport"). Kept below for
   the reasoning, which outlived the task. Everything the item lists as "removes"
   is gone, and `test_no_module_outside_the_seam_imports_accudisc` did become
   trivially true and was kept, as planned. The sequencing worry ("retiring before
   the install lands leaves cdda2img unrunnable anywhere the binding is missing")
   was resolved the other way: a missing binding is now **fatal by design**, and
   so is an ABI mismatch. Original item:

   **[C2I] Retire the subprocess transport entirely** (Keith's ruling, 2026-07-29).
   Sequencing is load-bearing — retiring before the install lands leaves cdda2img
   unrunnable anywhere the binding is missing. Removes: the subprocess arm of
   `accudisc_read()`, `_run_write_subprocess`, `CDDA2IMG_ACCUDISC_TRANSPORT`,
   `_try_binding`'s decline path, `active_transport()` in the RLOG engine line, the
   `AbiMismatch`→degrade path (becomes a hard raise), exit-code translation, and both
   two-carrier instruments (`tools/disc_ab.py`, `tools/binding_ab.py`).
   **The largest single piece is `conftest._pin_accudisc_transport`** — the whole suite
   currently runs against the carrier being deleted, not the env var.
   `test_no_module_outside_the_seam_invokes_accudisc` **stays**: it becomes trivially
   true, which is exactly when a guard is worth keeping.
   The acceptance instrument is deliberately being deleted with it; the counter-argument
   was put and the trade taken, because the carrier question is settled on banked
   evidence (112.69 s vs 112.75 s, PCM and C2 byte-identical).

#### Blocked on media

7. **[K] A real burn.** `write_disc` is exercised only against a CDEmu virtual writer —
   that proves the return path, byte layout and TOC grammar, not laser timing, DAO
   lead-in or media quality. Needs a blank CD-R. `--simulate` needs one too.
8. **[K] One-sided (pre-boundary) static-Q clustering** — needs a disc with a denser
   static population than Tracy's 1314. See the dedicated item below.

#### Independent of all the above

9. **[C2I] B-7 menu alternatives UI** — the confirmed destination for the trust model.
   The §11.5 prerequisite is half-shipped (`Resolution.contenders` + `skipped`,
   `6154526`); what remains is routing the adapter's invalid-ISRC / "Unknown Artist"
   drops through `skipped`, which needs propose-then-skip instead of filter-first.
   **Wants a design conversation before code.**
   **Design conversation started 2026-08-05 — see the "B-7 design input" block under
   N1e**: kgr's proposal is to feed the per-candidate MB `disambiguation` strings
   (plus each candidate's MB→Discogs link) into the alternatives menu. Note the
   **granularity gap** recorded there — `contenders`/`skipped` are per-*field*,
   whereas five pressings agreeing on every field and differing only in
   `disambiguation` are alternatives of a *record*, one level up. That gap may be
   why B-7 has stalled. N4 (`release_selected_via` naming the wrong rung) is
   exactly the PROV-string defect B-7's second half exists to retire.
10. **[C2I] B-4 post-soak cleanup** — delete the legacy merge chain. Needs the two
    mid-pipeline consumers decoupled first; retires `test_shadow_equivalence`.
11. **[C2I] Make `ortools` optional.** The remainder of the dependency-hygiene item
    (`textual` and `wayback` closed 2026-07-30, below). It is the heaviest wheel in
    the set and serves exactly one thing — the `best` batching strategy — through a
    top-level import in `input_selector.py`, so every user installs a CP-SAT solver
    whether or not they ever pick that strategy. Making it an extra means making the
    import lazy first; `depcheck.RUNTIME_PYTHON` would then need a notion of
    "declared optional" rather than the flat required list it has now.
13. **[K] What does `FLAG_MASTER_MODE` mean? — OPEN DISCUSSION, no code pending.**
    Raised 2026-08-03, parked for thinking time. The bit's current meaning is
    "`create` was run with `--silence notrim`", and the discussion is about whether
    that is the right thing for it to mean. **Nothing has been removed and no code
    is queued** — the aim is to clean up redundancy and tighten definitions.

    **`--silence` stays.** Retiring it was proposed and withdrawn the same day, on a
    point that is worth keeping written down: a disc **rip** has native pre-gaps, so
    trimming and re-padding one is destructive and breaks AccurateRip — but a
    `create` from loose audio files has **no pre-gaps at all**, so the tool has to
    synthesise them, and trimming the source waveform is how the resulting gap is
    controlled. The feature is not "alter the audio", it is "author a gap structure
    that does not otherwise exist". It never applied to `rip` or `import` in the
    first place (`create_image` is its only caller, `cdda2img.py:745`), which is
    exactly why it is safe. The redundancy to clean up is in what the *flag* claims,
    not in the feature.

    **Where the discussion got to.**
    - The first redefinition proposed was REMASTER = `create` **or** destructive
      normalisation; MASTER = rip/import without it. The second clause turned out to
      be unreachable: `--normalize` is **extract-time only** (`container.py:802`) —
      it derives a gain factor and applies it to the FLACs on the way out, leaving
      the stored PCM untouched and the container's flags word already written. kgr's
      call on learning this was to leave `--normalize` where it is, which is right:
      nothing in the pipeline alters stored audio, and that is the guarantee worth
      keeping. So the clause was dropped rather than wired.
    - That collapses the definition to **REMASTER = `create`, MASTER = `rip` |
      `import`** — which is a pure function of the PROV `mode` key, already in every
      container. The bit then has **two writers for one fact**, and two writers that
      can disagree is a defect waiting for a refactor. If this is the answer, derive
      the bit from `mode` in one place.
    - Note this is where the redundancy actually sits. With `--silence` retained, the
      bit today distinguishes two *kinds of `create`* — trimmed vs not — while PROV
      `mode` already distinguishes `create` from `rip`/`import`. So a reader has one
      field that answers "was this authored?" and a second that answers "and if so,
      were its gaps synthesised?", but the second is spelled as though it answered
      the first. Whatever the bit is redefined to mean, the `--silence` choice is
      still worth recording somewhere — it is a real fact about how the audio was
      assembled — and PROV is the cheaper home for it than a header bit.
    - kgr then observed that an import cannot be *guaranteed* a master either — we
      did not make the image, do not know what tool did, and cannot see whether it
      was processed — and proposed gating import's MASTER claim on passing
      AccurateRip. Also that the states should stay **binary**: not recognised
      implies remaster, so no `UNKNOWN`.

    **The unresolved objection** (mine, recorded so the discussion does not restart
    from zero). Absence from AccurateRip is not evidence of alteration: AR is a
    database of pressings *other people have submitted*, so silence from it means
    obscure pressing, promo, limited run, CD-R, or simply nobody got there first.
    None of those is an alteration of the audio. Three consequences:
    - the label would depend on **the network** — the same disc imported offline
      reads REMASTER, online reads MASTER — and it is frozen at build time, so a
      disc that enters AR next year stays REMASTER forever;
    - one bit cannot carry both *did we alter it* (always knowable, our own action)
      and *does it match a known pressing* (sometimes knowable, someone else's
      database). Collapsing them makes a `create` container and an unverifiable rip
      read identically, and those are very different provenances;
    - confirmation is **already recorded, and better**: the ARIP block carries
      per-track v1/v2 CRCs, confidence and status, and PROV carries
      `arip_transport` / `arip_dbar_b3sum`. That distinguishes "12/12 at confidence
      200" from "2 tracks not in the DB" from "verified and failed" — all of which a
      single bit collapses to one word.

    Note also that import does **not** run AccurateRip today (`verify_rip` is called
    only from `rip_image`; `_finalize_import` takes `arip_block=None` unless the rip
    path supplies it), and a foreign image has **no known offset**, so the gate would
    need an offset *sweep* to find where AR matches — the same machinery as CTDB's
    `select_entry`. Feasible, but real work, and cheap for `rip` while expensive for
    `import` — the opposite asymmetry to where the gate was proposed.

    **The alternative on the table**: keep the bit for alteration only (REMASTER =
    `create`, always knowable, no network, no `UNKNOWN`), and if imports deserve
    suspicion say so as provenance rather than as a fidelity claim — `mode=import`
    already means "this capture is asserted by the source image, not observed by
    us". If AR is to gate anything, gate a separately-named claim
    (`ar_verified=yes|no|unavailable`) where `unavailable` stays honest instead of
    being folded into a negative.

    Whatever is decided: `rbi_spec.md` defines bit 2, so **spec before code**. The
    bit's *value* does not change, only its meaning, so this is a spec-doc edit and
    not a format bump. Worth settling at the same time whether "master/remaster" is
    still the right vocabulary — under every proposal above the bit is closer to
    *"the stored PCM is not a verbatim capture"*.
14. **[AD, we supply evidence] Their task 0 — RECOVERED sectors returned wrong, 9/9.**
    Our report, their `[P1]`, still undiagnosed. H3 (coordinate-system mismatch) was
    eliminated 2026-07-29 from artefacts we hold: the shift is exactly one sector, the
    read offset is +30 samples = 0.051 sectors, and the CTDB image-domain shift is zero
    on Tracy because track 1 carries no `START` line. Field is H1 (storage off by one)
    vs H2 (`RECOVERED` simply false). Discriminator handed to them and device-free:
    they report **10 flagged map bytes for 9 corrupt sectors** — where the tenth sits
    separates the two.

    *(There is no item 12; the list has always jumped 11 → 13. Noted 2026-08-12 so
    the gap is not mistaken for a lost entry.)*
15. **[C2I] `speed_bands` shipped and nobody has looked at it** — noted 2026-08-12,
    no work queued. The binding now publishes `features = {caller_map_buffers,
    speed_bands, speed_honoured, subq_map}`, and `SpeedRung` carries
    `bands_cx` alongside `measured_cx` / `min_cx` / `max_cx` / `equiv_x` /
    `verdict`. Two things it touches, neither audited:
    - **The standing ladder-policy question** (`accudisc_reader.py` docstring,
      and CLAUDE.md's "do NOT replace `drive_speed.admitted_ladder` with the
      binding's"). Our rules 2 and 3 exist for drives whose page 2A does not
      report; per-band rates are a *third* kind of evidence and may make rule 3
      (bare `measured`) redundant. That would be a policy change with its own
      evidence, so it is a measurement first, not an edit.
    - **A prediction we currently owe AccuDisc.** Outbound §169 argues their
      ABBA ceiling of 42,09x uses an *address* reference their own band model
      contradicts, and offers a falsifiable test: under a fixed-radius reference
      the 40x rung on Tracy is capped near 31,4x and its **outer band** should
      flatten there rather than climb toward 40. `bands_cx` is that quantity, so
      the test may be runnable here rather than waiting on their reply. Their
      measured 23,68x at req=40 is a whole-disc average and is **not** the
      quantity — the outer band alone is.

### ✅ DONE 2026-07-30 — Dependency pre-flight: `cdda2img doctor` + a runtime gate

Both halves of Keith's ruling ("check, do not install"): `cdda2img doctor` reports
every dependency with what each missing one would have enabled and the command that
would install it, and a short form of the same check runs before every other
subcommand and refuses to start, naming *all* the missing packages at once instead of
one `ImportError` at a time. Neither installs, downloads, or touches the network.

The load-bearing finding is that **the check cannot live behind the application
import**: `import cdda2img.cdda2img` eagerly pulls in `av`, `mutagen`, `numpy`,
`ortools` and `unidecode` (measured), so an argparse subcommand would have raised
`ImportError` before reporting anything — able to diagnose every dependency except the
ones actually missing. `cli.py` and the new `depcheck.py` are therefore standard-library
only, enforced by a test that reads `depcheck.py`'s own AST, because an import-based
test passes trivially in an environment that has everything.

Verified on a genuinely bare venv (`pip install --no-deps .`, nothing else): all twelve
reported in one run, exit 1. Three states, not two — a binding reachable only via the
git-ignored `tools/accudisc/pybinding` shim reports `WARN`, never `ok`, since that is
the exact false pass that left the binding transport inert for two days.

Swept up with it: `textual` dropped from `[project.dependencies]` (imported by nothing
shipped), `wayback` moved to the dev group (ty type-checks `tools/`, and `uv sync`
installs only `dev`), the `track_preview.py` cd-paranoia line in `CLAUDE.md` corrected,
and the stale dependency sections of both `README.md` and the man page rewritten —
both still named cdrdao and cd-paranoia as the rip path and marked `ffmpeg`/`fpcalc`
required, when PyAV carries FFmpeg in its own wheel and AcoustID gates itself.

### ✅ DONE 2026-07-29 — Full API migration: every call is on the binding

All three calls (`read_disc_c2`, `write_disc`, `speed_ladder_rows`) are on the
binding, and it imports in this project's own 3.10 venv — AccuDisc shipped an abi3
extension (`py_limited_api`) so one artefact serves 3.10-3.14. Validated: whole-disc
A/B/A (binding 112.69 s vs subprocess 112.75 s against a 3.68 s noise floor, PCM and
C2 byte-identical), ladder cross-check to <=0.02x on all seven rungs, CD-Text capture
byte-identical across transports, and a CDEmu burn round-tripped byte-identical.
Remaining: a burn on physical media (needs a blank CD-R).

#### Original item (2026-07-27)

**Draft. Work starts next session.** The three calls left on the subprocess in the
2026-07-27 default flip (`read_disc_c2`, `write_disc`, `speed_ladder_rows`) must move
to the API. AccuDisc's agent has committed full cooperation, and changes on both sides
are in scope.

> **PREMISE CORRECTED (AccuDisc §bz.1) — the CLI is NOT being retired.** This item was
> first written as "the CLI is going away, so the fallback goes with it". That was an
> over-read of a one-line decision, and AccuDisc declined to sequence on it, checked
> with Keith, and came back with the verbatim answer:
>
> > "I'm deprecating use of the CLI for cdda2img. The whole purpose of the API is that
> > *all* consumers use it exclusively. The CLI is *our* consumer of the API. Everyone
> > else creates their own."
>
> So the CLI stays — it is AccuDisc's **reference consumer**, their standing proof that
> the public header is sufficient to build a real tool against. What is deprecated is
> *our* use of it as a transport. The migration below is unchanged; one consequence
> drawn from the stronger premise was wrong, and is corrected under "The reprieve".
>
> It also does **not** overturn their `CLAUDE.md` — the contradiction that made them
> check was a real signal, not a stale doc. The decision sharpens that line.

**The good news, established by reading their header rather than assuming:** all three
already exist in C. This is binding-layer work on their side, not new engine features.

| ours | C entry point | bound today? |
|---|---|---|
| `write_disc` | `accudisc_write(dev, toc, bin, opts, progress, user)` | no |
| `speed_ladder_rows` | `accudisc_probe_speed_ladder(dev, lba, count, cands, n, out)` | no |
| `read_disc_c2` | `accudisc_read()` + `Device.read_to_file()` | **yes**, see below |

#### The three questions — asked as §106.3, **all answered from source** in §by

These were the "type-correct, reference-wrong" candidates: each produces plausible
numbers on the wrong referent if guessed. Answers below are theirs, from their tree.

1. **The `speeds` span is DERIVED, not a constant** (§by.1, `cli/main.c:659-664`):

   ```c
   lba   = start >= 0 ? start : toc.leadout_lba / 4;
   count = toc.leadout_lba > lba ? toc.leadout_lba - lba : 0;
   if (count > toc.leadout_lba / 2 && start < 0) count = toc.leadout_lba / 2;
   ```

   The default is the **middle half of the disc** — start at 25% of `leadout_lba`,
   span 50% — chosen for representative CAV radius plus headroom for one fresh window
   per rung. An explicit `--start` deliberately skips the clamp and runs to lead-out.
   **Reproduce the expression, not a number read off one run**: it is a function of
   `leadout_lba`, so a hardcoded span drifts from the bench archive on every disc of
   a different length, and every rung stays a plausible number while meaning something
   else. Default candidate rungs `{52,48,40,32,24,16,8,4}` filtered to ≤ page-2A max.
   Otherwise the mapping is clean: `requested_x`/`reported_x` → the §9.3
   `req == page2a` rule; `measured_cx` is **centi-x** (531 = 5.31×) where our regex
   parsed a float.

2. **Not-blank: agreed, deferred, and the real argument is better than ours** (§by.2).
   We argued "they are different events". They checked whether the CLI's
   `result=not_blank` is a guess and found it is not — `ACCUDISC_ERR_UNSUPPORTED` is
   reachable from `accudisc_write` in exactly one place, the blank check. But that is
   exact **by census, not by construction**: any future `ERR_UNSUPPORTED` under the
   write path silently joins "not blank", and the failure is well-formed on both sides
   — they report not-blank for a blank disc, we tell the user to insert a blank disc
   they already inserted, and neither test suite notices. `ACCUDISC_ERR_NOT_BLANK =
   -13` is free and touches three places; it lands when the retirement question
   resolves. **Until then keep keying on the `result=not_blank` token** — it is what
   they would fix first, and our current code needs no change.

3. **Q3 dissolves — there is no inline mechanism to lose** (§by.3). There is no
   cross-call lead-in cache (three calls are three reads), *but* the CLI's
   "single-spin capture" is not a fold into `accudisc_read`: it makes two ordinary
   public calls (`accudisc_read_full_toc`, `accudisc_read_cdtext`) before the read, on
   the same open handle. "One spin-up instead of three" is a property of **one process
   holding the handle open**, not of any combining in the library — the three spin-ups
   came from three subprocess invocations each opening and closing the device. So a
   binding consumer holding **one `Device`** across `read_full_toc_raw()` →
   `read_cdtext_raw()` → `read()` issues an identical command sequence.
   `Device.__init__` calls `accudisc_open` and nothing else, so there is no hidden
   lead-in access either. **Two rules, both free**: keep the order (lead-in metadata
   before the audio read, so the head is not sent to the program area and back), and
   **do not open/close between them** — that, and only that, is what the subprocess
   cost. Reintroducing it would be invisible: every byte still correct, three spin-ups.
   They offered a test pinning the command sequence; take them up on it.

#### `read_disc_c2` is the one we can start on now

`Device.read_to_file(lba, count, pcm_path=, c2_path=, sub_path=)` already does the
stream splitting. Its own docstring prefers the CLI for a whole disc because the CLI
"writes the file inside the library's address space, whereas this routes every sector
through Python first" — **that premise dies with the CLI**, so either they add a
library-side whole-disc-to-file entry, or we accept one memcpy per chunk. That cost is
measurable, not arguable: rip a disc both ways and compare wall-clock. Measure before
asking them to build anything.

#### Sequencing

AccuDisc's queue is `speeds` min/avg/max, then binding `accudisc_probe_speed_ladder`
(their §bx). **Their live blocker** (§by.4): `accudisc_speed_rung` has no `size` field
and min/avg/max grows it, so binding the probe first would close the free ABI-break
window and turn a routine field addition into a versioned break for us. **Keep the
regex until they say explicitly that it can go.**

So: (a) measure `read_to_file` on a whole disc while they work, (b) ~~send the three
questions~~ — done, all answered in §by, (c) take `speed_ladder_rows` when the binding
lands, (d) `write_disc` last — it is the destructive path, the only one where a wrong
answer damages media, and it deserves the `--simulate` gate exercised on both
transports before the binding one carries a real burn.

#### The reprieve — we do not lose the instrument after all

The first draft of this item spent a paragraph on the cost of losing byte-level
cross-transport comparison once the subprocess path went: every claim afterwards
resting on absolute gates (AccurateRip/CTDB) alone, a real reduction in what we can
measure about their library, to be "spent deliberately rather than discovered".

**That cost is not incurred** (AccuDisc §bz.3). Deprecating a transport for production
is not deleting the binary it drove, and the CLI is staying. So `tools/binding_ab.py`
keeps its A side indefinitely: one A/B harness that shells out, invoked deliberately,
against a production path that never does. Keep it, and keep its hard transport pin —
the pin is what makes it an instrument rather than a self-comparison, and that was
already true before any of this.

Still true: **do not remove the subprocess code paths until all three are migrated and
A/B'd on media.** The reason is now "the acceptance instrument needs them", not "we are
about to lose them".

### ✅ DONE 2026-07-29 — Phase E: modules already gone; the real find was the backup manifest

All §3 modules were already deleted and `sync.py` no longer warns. Checking that turned up
worse: `scripts/backup.manifest` enumerated paths and had fallen ~70 files behind — 25 of 57
source modules (including `accudisc_reader.py`), 44 of 58 tests and 38 of ~40 tools were
outside every backup. `_load_manifest` now expands globs; 97 files -> 211.

#### Original item (2026-07-26)

The last unfinished phase of `accudisc-migration-plan.md`. §9 (validator, profiles,
strict config) is complete and the snapshot pin is retired, so the soak condition is met.
Delete the §3 modules, update CLAUDE.md, close the plan.

`scripts/sync.py` already prints `Skipping missing: cdrdao_ripper.py` and
`Skipping missing: disc_reader.py` on every run — its manifest still lists modules that
were deleted, which is the tidy-up this phase is for.

### ✅ DONE 2026-07-29 — §9.3 speed ladder: admission now keys on AccuDisc's verdict

Shipped. `req == page2a` gave `[48, 40, 32, 24, 8, 4]` on Tracy; the verdict rule gives
`[40, 32, 24, 8, 4]`, matching AccuDisc's own `ladder admitted=` line. Three rules in
priority order (verdict, then `req == page2a`, then `measured`), gated on "some row was
**judged**" rather than "some row has a verdict" — `unknown` is truthy, and
presence-gating would send a `points=1` probe to an empty ladder and the degrade guard.

#### Original item (2026-07-26)

Documented in `accudisc-migration-plan.md` §9.3 and the `drive_speed.admitted_ladder`
docstring. **No fix shipped yet, but the design question is closed** — Keith ruled it
with whole-disc measurements, which is the evidence every earlier round lacked.

**The original defect.** With the SpeedRead uncap (0xE9) on, page 2A advertises the
drive's **data** ceiling of 48× and `accudisc speeds` returns `req=48 page2a=48` next to
`req=40 page2a=40`. Both pass the strict rule, so both are admitted as separate rungs —
but CD-DA is governed to 40× on this drive, so they are one speed wearing two labels.
`req == page2a` cannot detect it: both operands derive from the same advertised ceiling,
so the equality cross-checks the drive's *quantiser*, never its ceiling.

**Whole-disc reads settle it** (Keith, uncap on, `--start 0 --count 162892`). A full-disc
read covers every radius identically at both settings, so the CAV confound that wrecked
the `speeds` comparison cancels exactly:

| req | seconds | sectors/s | whole-disc avg | C2 sectors | C2 bits |
|---:|---:|---:|---:|---:|---:|
| 48 | 91.5 | 1780.3 | **23.74×** | 63 | 1160 |
| 40 | 89.8 | 1814.1 | **24.19×** | 54 | 1133 |
| 32 | 113.1 | 1439.8 | 19.20× | 1 | 12 |
| 24 | 150.2 | 1084.8 | 14.46× | 2 | 24 |

48 and 40 differ by **1.9%**, with 40 marginally *faster* — noise. 40 and 32 differ by
**20.6%**. The C2 counts corroborate independently: 48 and 40 flag 63 and 54 sectors over
the same span (~112,320–115,694), while 32 flags **one**. So 48 and 40 are the same
physical read and 32 is a genuinely distinct rung. The residual we spent §97–§99 chasing
was 0.45× ≈ 2%, not a real anomaly.

**Keith's ruling, which is the specification.**

1. CD-DA is never read at 48× on this drive. Ever.
2. Page 2A correctly reports the requested speed *ceiling* — it is not lying, it needs
   reading in context.
3. That ceiling is 48 because `speed-uncap` is on.
4. Actual CD-DA speed is still governed to 40×.
5. `speeds` transfer rates are **mid-disc only**; CAV rates differ at both ends. He has
   asked AccuDisc to report min/avg/max instead.
6. `speed-uncap off` changes only the *data*-disc maximum and the displayed `speeds`
   output. Max CD-DA stays 40×.
7. **`speeds` output varies with disc degradation** — the governor has been observed
   capping at 32× and as low as 8× on damaged media, with the uncap having zero
   influence. Throttled speeds are real and measurable, not a page-2A artefact.

**The algorithm to implement in `drive_speed.admitted_ladder`:**

> Read the page-2A settings on the ladder → measure actual throughput at **all three
> disc regions** (beginning, middle, end) → decide which page-2A readings represent
> speeds actually achievable under the governor → discard obvious duplicates and
> unachievable rungs → the remainder is the real ladder.

This supersedes both earlier candidates. It is not "trust `measured`" (a single mid-disc
figure carries a radius term) and not a media-class floor (a policy that bakes one
drive's manual into the rule). It is: measure across the disc, then dedupe on
achievability. Because the governor is disc-dependent, **the ladder must stay per-disc
and must never be cached per drive** — already the rule, now with a mechanism.

**Fidelity is not at stake.** No speed setting can sabotage audio extraction; nothing can
force this drive to read CD-DA at 48×. But a 40× read of a *damaged* disc produces many
more Q and C2 errors (63 flagged sectors at 40–48× vs 1 at 32×), so **slower is optimal
for damaged media** — which is the recovery-ladder result arrived at from the hardware
side.

**Blocked on:** AccuDisc's min/avg/max `speeds` output (Keith has requested it). Until it
lands, cross-rung `measured_cx` carries an uncorrected radius term — each rung is probed
at its own window, `stride = count/ncand`, windows marching outward with a descending
candidate list, so the fastest rungs are always measured innermost. A second confound
applies to any rule spanning the full ladder: timed window length is `min(req*75, 2250)`,
so 48/40/32× are length-comparable at 2250 sectors but 24× times 1800, 16× 1200, 8× 600,
4× 300. Scope any collapse rule to length-comparable rungs.

### ✅ DONE 2026-07-29 — reconciled; past runs fenced, not re-indexed

`tools/recovery_bench.py:probe_ladder` now delegates to `drive_speed.admitted_ladder`,
which also stops it bypassing the seam (it spawned `accudisc` directly, so after the
binding migration it would have measured the subprocess while the rip path measured the
library). **Past runs are NOT re-indexed** — relabelling archived measurements changes
what they mean, silently. New runs carry `ladder_rule = 'verdict@2026-07-29'` in the file
header; runs without that key predate the change and are fenced by its absence.

#### Original item (2026-07-26)

`drive_speed.admitted_ladder` admits `page2a` only where `req == page2a`;
`tools/recovery_bench.py:probe_ladder` admits **every** non-zero `page2a` regardless of
`req`. They agree on every row set observed so far and diverge as soon as a quantised
row yields a ceiling no other row reaches (`req=16 → page2a=10` with no `req=10` row:
dropped by the library, admitted as 10 by the bench).

The bench is arguably the more defensible — it labels the rung by the ceiling the drive
*accepted*, so nothing is mislabelled — but they were never reconciled deliberately.
**Reconciling them retroactively changes what past bench runs mean**, since every result
is indexed by the bench's ladder. Decide the rule first, then decide whether old runs
are re-indexed or fenced off.

### Watch for AccuDisc's uncap-probe interface change (2026-07-26)

Proposed to Keith on their side, not committed, and it lands on us automatically now the
snapshot pin is retired (`tools/accudisc/accudisc` symlinks into their live build):

1. `-q` no longer suppressing data-integrity warnings — currently `if (!quiet)` gates the
   driver-independent uncap warning, and we pass `-q` at `accudisc_reader.py:335`, `:439`
   and `recovery_bench.py:777`. The flag that makes us a machine consumer is the one that
   suppresses the hazard notice written for us.
2. A **machine-readable token** for it, since stderr prose is not an interface we key
   decisions on (LINT/§80.2 rule).
3. `speed-uncap` query falling back to the page-2A probe when no driver is attached,
   reporting `on` / `off` / `likely-on` / `unknown` — needs neither a driver nor
   `cap_sys_rawio`.

**No guard is being built on our side** (2026-07-26 decision, manual-backed: CD-DA is
throttled to 40× regardless of the uncap, so audio is unaffected). If (3) lands, a
three-way pre-flight becomes cheap and honest — refuse on `on`, proceed on `off`, record
the caveat on `unknown` — but it is not needed today.

### CLOSED 2026-07-29 (unanswerable here, and unused) — is page-2A `max_x` media-class invariant?

AccuDisc's `stock_ceilings` classifier is keyed on **(model)**; the PX-716A manual (p.15)
publishes three ceilings for that model by **media class** — 48× data, 40× CD-DA/CD-R
audio, 32× CD-RW audio. If page 2A's `max_x` tracks the loaded media class, a
single per-model stock number is coarser than the drive's real behaviour and the CD-RW
audio case is untested by either project.

**Closed rather than deferred, on two grounds.** (1) Keith has no blank CD-RW and is not
planning to obtain one, so it is not answerable on this bench — and an open item nobody
can act on is worse than a closed one with its reasoning written down. (2) More
importantly, **the rule we ship does not consume the answer.** `drive_speed.admitted_ladder`
rule 1 admits on AccuDisc's *verdict*, which is a measured rate comparison at three radii
— it never reads `max_x`. Page 2A only enters at rule 2 (`req == page2a`), which fires
solely when the engine reports no verdict at all. So on any AccuDisc build that judges
rungs, a media-class-dependent `max_x` cannot reach our ladder.

**Residual, stated rather than tracked:** should we ever fall through to rule 2 with a
CD-RW in the tray (an older engine, or a `points=1` probe where nothing is judged), the
ladder may admit rungs above the 32× CD-RW audio ceiling. The measured-rate rule 3 and
the outcome guard both sit below it, and a rung the drive silently refuses shows up as a
duplicate rate rather than as a wrong number. Not worth a guard for a case that needs an
absent disc *and* a downgraded engine.

Originally offered to AccuDisc in outbound §88.2 (which claimed "we have the discs" — that
was wrong about CD-RW specifically). Closure relayed in §121; it also lands on their task 5
stock-ceiling A-vs-B discriminator, which is unanswerable on this bench for the same reason.

### Never established: one-sided (pre-boundary) static-Q clustering (2026-07-26)

RECOVERY.md §12.9.2. Symmetric ±150 boundary clustering is **refuted with power** on
Tracy Chapman. The *pre-side* variant is neither confirmed nor refuted: Tracy's pre-20
window expects 1.61 frames (no power), and ABBA's original z = +3.50 was a normal
approximation at λ ≈ 1.6 — exact Poisson gives p = 5.8e-3, which survives no
multiplicity correction on a post-hoc window.

Testing it needs a disc with a **denser** static population than Tracy's 1314, and a
pre-window width pre-registered before the captures are taken. Low priority: the decile
result (§12.9.3) already says more about the population being physical than a boundary
test can.

### ✅ DONE 2026-07-25 — CD-Text titles shift by one when a track has no title (2026-07-24)

**Fixed.** `cdtext.py:_decode_strings` was rewritten to re-sync to each pack's declared
track number while walking the stream (accumulate-then-commit), so a pack header is
honoured even mid-string. Resyncing only at string *boundaries* is a trap that yields a
different wrong answer (gap reported at track 16) — AccuDisc's warning caught that before
it shipped. Regression tests added in `tests/test_cdtext.py`
(`test_track_with_no_string_does_not_shift_the_rest`,
`test_continuation_pack_header_overrides_the_running_count`); the test helpers were also
fixed, since they had been writing track `0` into continuation packs, which no real disc
does. The encoder half (cdrdao dropping U+2010) is closed separately by `fold_cdtext` plus
the AccuDisc transition removing cdrdao from the burn path.

Original analysis follows.


Found by the Step-D burn verification (report:
`private/research/incoming/burn-verify-abba-gold-20260724.md`). The CD-R under test carries
**no CD-Text TITLE for track 13**; `cd-info` and libmirage both read track 13 as
PERFORMER-only. Our decoder **compacts the gap**: every title from 13 onward shifts up one
and track 19 becomes a duplicate — 6 of 19 titles wrong, silently, on a disc that reads
clean. "Voulez-Vous" appears nowhere in the output, so this is a positional shift, not a bad
metadata match.

**Two separate defects, don't conflate them.** *Why* the gap exists: the source RBI's track
13 title is `"Voulez‐Vous (edit)"` with **U+2010 HYPHEN**, unrepresentable in CD-Text block 0.
The encoder that discarded it is **cdrdao's** — the original §10 bug, observed end-to-end on
physical media (AccuDisc has no string→pack encoder; libmirage measurably round-trips U+2010
intact; the blob's 504 payload bytes contain zero non-ASCII). Our `fold_cdtext` fix stops us
handing cdrdao unencodable characters, and the AccuDisc transition removes cdrdao from the
burn path entirely, eliminating the class. *This* item is the decoder half — a gap must never
shift the other titles, whatever caused it — and is worth fixing independently.

**Root cause confirmed** against the disc's raw packs (`accudisc cdtext`, 760 B): the TITLE
(0x80) stream holds **18 strings for 19 tracks with no empty string and no NUL placeholder**
for track 13 (`…Fernando\x00Gimme! Gimme!…`). Sequential NUL-counting therefore *cannot*
land correctly. The absent track is visible only in the **per-pack track number** (pack
byte 1), which names the track whose string starts in that pack; on this disc those run
`… 9 9 11 12 14 14 14 14 15 15 17 17 17 18 18 19` — the pack beginning the final string
declares 19, so a header-driven decoder ends on Waterloo=19 while ours ends on Waterloo=18
plus a duplicate. `cd-info` and libmirage both re-sync per pack and both get it right.

`subq_toc.py:136` maps by track *number* and is not the culprit. The defect is
`cdtext.py:_decode_strings` (241-259), which takes `first_track` from the first pack and
then counts sequentially. **Fix:** re-sync to each pack's declared starting track while
walking the stream. Regression fixture: capture this disc's packs into `tests/fixtures/`
(no sparse-title capture exists today) — note it is a commercial pressing's CD-Text, so
keep the fixture minimal (the TITLE packs around the gap suffice).

### ✅ DONE 2026-07-25 — CTDB repair was structurally impossible on a disc whose track 1 starts at LBA > 0

Found by investigating why CTDB declined on the 2026-07-25 ABBA *Gold* rip (it was the
first CTDB failure ever seen). Two defects, one trigger: CTDB's parity and per-track CRCs
cover `[first-track INDEX 01, lead-out)` while our PCM spans `[0, lead-out)`, and the two
coincide only when track 1's INDEX 01 is at LBA 0 — true of every test disc we had.

`ctdb_repair.track_crc_at` derived `laststride` from the PCM buffer, over-trimming the last
track's CRC window by 1,764 sample-pairs (outside the ±700 sweep, so the gate could never
pass); and `ctanalyse` accepted `--toc` and silently ignored it, shifting its RS grid by 3
whole strides and emitting 7,375 confident-but-wrong corrections against a provably perfect
rip. The CTDB CRC gate rejected them, so no audio was harmed.

Fixed: domain-correct `laststride` (both copies), `ctanalyse` honours `--toc` and reports
`image_first_frame`/`image_frames` so a stale binary is refused rather than trusted,
`verify_ctdb` returns per-track roles (unfixed/regressed/abstained), `ctdb_declined` reaches
PROV, and a failed erasure-assisted run falls back to error-only. New
`tests/test_ctdb_repair.py` (15 tests, every geometry fixture uses `bounds[0] != 0`) — there
was no test file for this module at all. Analysis:
`private/research/incoming/ctdb-failure-abba-gold-20260725.md`.

**Closed 2026-07-25 — the C2 erasure-bitmap origin shift is now measured, not merely
derived.** It had never been *executed* with a non-zero origin, because every fixture and
every repaired disc had `bounds[0] == 0`, where the shift is a no-op.
`tools/ctdb_erasure_origin_test.py` runs it on ABBA *Gold* (`bounds[0] = 33`, CTDB entry
829896, npar 8): 6 words damaged in a single RS column — above the error-only capacity of
`npar/2 = 4`, below the erasure capacity of `npar = 8` — so the two paths must disagree.
Correct bitmap → repaired **bit-exactly**; no bitmap → refused ("damage exceeds RS
capacity"), proving the erasures did the work; bitmap displaced by `word_base` (the exact
error a broken origin would make) → refused, proving the harness can see the fault it is
looking for. Two unit tests now pin the domain contract from both sides
(`build_erasure_bitmap` emits over `[0, lead-out)`; `repair_whole_disc` sizes it from the
PCM buffer), because the hazard is a future "fix" that narrows the Python side and applies
the shift twice.

### ✅ DONE 2026-07-29 — diagnosed (not silently repaired)

`_ar_has_total_mismatch` + `_diagnose_total_ar_miss` run `detect_offset` on an all-tracks
miss and record `ar_total_miss` / `ar_offset_candidates` / `ar_offset_suggests` in PROV.
**Audio untouched by design**: a widely-pressed disc verifies at several offsets at once, so
picking the winner would be choosing one pressing's cohort and calling it truth.

#### Original item (2026-07-24)

Same report, §2b. The Step-D CD-R readback contained **15 stereo samples of a transient read
error** at LBA 327495 that two re-reads did not reproduce. Whether C2 flagged it is *not
known and cannot now be established* — the `.c2` sidecar lived in the per-invocation
`TempFiles` mkdtemp and a fresh capture would measure a different read; do not assume C2
missed it (that would contradict the ~99 % precision banked from c2bench).
`_recover_failed_tracks` fires only on a *partial* AR mismatch; here the wrong
read offset (`+30` applied to a disc burned uncorrected on the same drive) made **every**
track miss, so the partial condition never held and no recovery ran. Consider: treat a
whole-disc AR miss as a trigger for an offset probe (AR `detect_offset`) before concluding
"not in DB", and/or let C2-flagged sectors trigger re-reads independently of AR. Worth
folding into the §9 recovery-profile work rather than patching separately.

### DONE 2026-07-25 — disc-not-blank now keys on AccuDisc's machine token

AccuDisc shipped it in `a76ede2` (`summary … result=not_blank`). `_run_accudisc_write` now
returns `(rc, stderr, result_token)` and `_write_disc` keys not-blank on the token, not on
stderr wording. The regression test deliberately supplies stderr that does *not* contain
"not blank", so the decision can only come from the token.

<details><summary>original item</summary>

### Switch disc-not-blank detection from stderr scrape to AccuDisc's machine token (2026-07-24)

`disc_writer._write_disc` currently distinguishes exit-2 disc-not-blank from exit-2
transport failure by `"not blank" in stderr_text.lower()`. That works but keys on stderr
*wording*, which AccuDisc's machine-interface contract explicitly reserves the right to
change (stderr is not a stable interface). AccuDisc agreed (their §t) to emit a machine
token on `--progress-fd` for the burn-didn't-start cases — `summary ... result=not_blank` /
`result=error`. **When that ships:** parse the `summary` line's `result=` in
`_run_accudisc_write` (it already reads the `--progress-fd` stream and ignores `summary`),
return it alongside `(rc, stderr)`, and key not-blank on `result=not_blank` — dropping the
stderr scrape. Interim stderr match stays until then (degrades to the generic exit-2 message
if the wording changes, so no burn misbehaves). Confirm the token's exact final spelling
(field order, whether `mode=burn` is always present) against AccuDisc before writing the parse.

</details>

### RESOLVED BY RETIREMENT — c2read deleted 2026-07-24; hazard recorded for reuse

**c2read is retired.** Archived to `private/deprecated/c2read-20260724.tar.gz` (13 entries)
and deleted from the tree: `tools/c2read/` (C tool), `src/cdda2img/c2_reader.py`,
`tests/test_c2_reader.py`, `tools/c2read_recovery_test.py`, `tools/c2bench.py`,
`tools/c2timing.py`, `tools/cx_census.py`, `tools/modepage_experiment.py`,
`docs/reference/c2read-upgrade-plan.md`. AccuDisc superseded all of it. 1273 tests pass
(−14 = the deleted wrapper's tests); `make check` green.

**Two tools kept that shelled out to `c2read` — retargeted to AccuDisc 2026-07-24** (they
were never c2read material, they merely used it as a capture engine). Both now route through
`cdda2img.accudisc_reader`:
- `tools/ctdb_repair.py` — `c2read --toc/--features/--full/--stop` → `read_toc` /
  `drive_supports_c2` / `read_disc_c2` + `park_spindle`.
- `tools/toc_parity.py` — `c2read --full` (sub + lead-in, no PCM) → `read_disc_c2` with
  `output_pcm`/`output_c2` omitted. That required widening `read_disc_c2` to make those two
  outputs optional (backward-compatible; test `test_read_disc_c2_metadata_only_omits_pcm_and_c2`).
  The cdrdao read-toc *reference* side stays — it is the parity gate's baseline until cdrdao
  itself is removed in a later migration phase.
Nothing in the tree references `c2read` any more.

The transfer-length hazard below is **no longer live for us** (no raw-SCSI code remains in
this repo) but is recorded because it is subtle, it cost AccuDisc three months, and it will
apply again to anything that issues SCSI here or reviews AccuDisc's.

#### The hazard (for reuse, not for action)

Found by AccuDisc (their §n retraction, fixed their side in `8bda198`) and **confirmed by
reading ours**. A two-step SCSI allocation that sizes the second transfer straight from the
returned data-length header can produce an **odd** length; ATAPI moves data 16 bits at a
time, so the host adapter rejects it **before the drive answers** — Linux `host_status =
DID_ERROR (0x07)`, driver `0x00`, **no sense**. It then surfaces as a bare I/O error and is
easily misread as a *disc-health* verdict rather than a *transfer* fault (that misreading
cost AccuDisc three months and produced a false `degrade=leadin_unreadable` on Stanley
Road).

A full TOC is `4 + 11*ndesc` with `ndesc = 3 (A0/A1/A2) + ntracks`, i.e. **`37 +
11*ntracks`** — **odd on every disc with an EVEN track count**. Stanley Road (12 tracks →
169) failed every run; an 11-track disc (158) never did.

Both sites in `tools/c2read/c2read.c`:
- `dump_toc_format()` — `len = ((hdr[0]<<8)|hdr[1]) + 2` (L221) → `scsi_in(..., buf, len,
  ...)` (L232). Exposes **format 0x02** (full TOC) on every even-track disc.
- `mode_sense10()` — `len = ((buf[0]<<8)|buf[1]) + 2` (L329) → `scsi_in(..., buf, len,
  ...)` (L333); `mode_select10()` inherits that `len` into `io.dxfer_len` (L363).

Parity by format (corrected by AccuDisc §o — our first pass said "only 0x02", which was
narrowly incomplete):
- **0x02 full TOC — exposed.** 11-byte descriptors → `37 + 11*ntracks`, odd on even track
  counts.
- **0x03 PMA — also exposed.** Same 11-byte descriptors, same `4 + 11*n` parity. Neither
  project issues 0x03 today, so it is theoretical — but it is the same trap if PMA is ever
  reached for during recovery.
- **0x05 CD-Text — structurally immune.** A blob is `4 + 18*npacks`; `18*npacks` is always
  even, so the length is always even regardless of pack count (including the 33/35
  ring-fill cases).
- **0x00 / 0x01 — immune.** 8-byte descriptors.

Fix is one helper (round any header-derived transfer length up to even) — AccuDisc's is
`adsc_alloc_even`. **Put it in the shared two-step reader, not per-format**, so every
format is covered regardless of which is used.

**Second-order trap, found in AccuDisc's fix by our review and fixed there in `f6494c1`:**
rounding then clamping re-introduces the bug — `len = even(len); if (len > cap) len = cap;`
hands an **odd `cap`** straight back out. Clamp to `cap & ~1u`. c2read's `mode_sense10()`
had exactly this clamp shape, and its `len` fed `mode_select10()`, so an odd cap would have
gone out on a **write** as well as a read.

**Observed consequence, measured on Stanley Road (PX-716A, AccuDisc `8bda198`):** the
even-rounded read returned **one byte more than the header declares** — file 170 B, header
`datalen=167` → declared total 169, one trailing pad byte (`0x2b`), `ndesc=15` = 3 pointers
+ 12 tracks. The pad is drive buffer residue, not data. Does not affect 0x05:
`4 + 18*npacks` is always even, so CD-Text needs no rounding and gets no pad — confirmed
empirically (a post-fix CD-Text re-capture of the same disc is **byte-identical** to the
pre-fix 148 B capture) and structurally on AccuDisc's side (`test_alloc_even` asserts no
CD-Text length pads for npacks 1..64).

> **Reporting this upstream fixed it at source, and found a second bug (AccuDisc `e9df8c7`,
> their §q).** (1) The pad was escaping as *data* — rounding is a **transfer** concern, but
> the padded figure was being reported as the **response length**. Now requests
> `alloc_even(len)` and reports `len`; the same disc dumps **169 bytes, pad 0**. (2) Found
> while re-reading that code: the clamp was `0xffff` (**odd**) *then* round → `0x10000`,
> which truncates to **0** in the 16-bit allocation-length field — a *zero-length* request,
> not a short read. Now clamps to `0xfffe`.
>
> So: "compare by header-declared length, not file size" remains the right defensive habit
> for any dump taken **before `e9df8c7`** (including our delivered
> `stanley_road__fulltoc_12tracks.bin`, 170 B — kept deliberately as a marker of the bug).
> From `e9df8c7` the artefact is clean at source and file size is a safe comparison again.

### ✅ DONE 2026-07-29 — quote-aware parse shipped

`mask_quoted()` blanks quoted-string interiors while preserving length exactly (offsets
are load-bearing — `parse_toc` slices per-track blocks by marker byte positions). Structure
reads from the masked view, values from the original text at the located offsets. A newline
inside an open quote is a hard `TocParseError`. `escape_toc_string` kept on the write side.

#### Original item (2026-07-24)

Found while cross-checking AccuDisc's parser injection report. `toc_parser.py` is
regex-based with `re.MULTILINE` and **no quote-context tracking**: `_START_RE`,
`_PRE_EMPH_RE`, `_COPY_RE`, `_INDEX_RE` anchor `^` per line and will match a line
sitting *inside* a quoted string.

**Exposure is the `import` subcommand**, which parses foreign cdrdao TOC+BIN images
that nothing of ours ever escaped. `escape_toc_string` is on the *write* side and gives
zero protection here.

Demonstrated so far: **field substitution only** (payload in a track's `CD_TEXT` block
changed the parsed title `'Normal'` → `'x'`). A phantom track, injected ISRC, pre-gap
and pre-emphasis were **not** achieved by that payload — but a five-minute payload is
not an audit and the structural susceptibility is real.

Fix shape (same as AccuDisc's): quote-aware scan; an unterminated quote at end-of-line
is a hard parse error. Start from AccuDisc's injected-TOC fixture. Their equivalent bug
produced a phantom track, shifted lead-out and attacker-chosen ISRC returned as OK.

**Keep `escape_toc_string` regardless of the audit outcome.** AccuDisc's quote-aware
parse has now shipped (`a619854`, their §2026-07-24i), so on the *burn* side it is
defence-in-depth rather than load-bearing — but on **this** (import/read) side it is
still the primary guard: `accudisc_write()` never runs on an imported foreign `.toc`.
Cross-project contract, recorded in `accudisc-migration-plan.md` §10.1.

### ✅ DONE 2026-07-29 — both parts, and part 1's premise had drifted

**Part 1 (default flip) shipped, but not as written.** The item says "c2read becomes the
normal-path primary read engine" — c2read was retired in July and AccuDisc is now the only
engine, so that decision had already been taken by other means. What actually remained was
narrower: `c2_recovery` now controls only whether the C2 bitmap is **written to a scratch
file**. C2 pointers are requested on the wire on every read regardless (`cli/main.c:1176`
sets `ACCUDISC_C2_PTRS` unconditionally; the binding matches), so the sector is 2646 B either
way and the read costs the same. `off` bought ~48 MB of scratch and gave up the erasure boost
that roughly doubles what ctanalyse can reconstruct. Default is now `auto`; `off` still honoured.

**Part 2 (pre-emphasis virtual disc) shipped, with the fixture corrected.**
`make_preemph_disc.py` flagged *every* track, which cannot distinguish a working detector from
one that always returns True. Tracks now alternate. CDEmu + `toc_parity` -> `PARITY: ALL MATCH`
against `cdrdao read-toc`, and the pattern itself asserted separately
(`[True, False, True, False]`) since agreement alone could mean both sides wrong together.

#### Original item (2026-07-05)

The user gave the go-ahead for both; deferred to next session for execution.

1. **Default flip — c2read becomes the normal-path primary read engine.** Change the
   `c2_recovery` default from `"off"` to `"auto"` (`config.py` `Config` default +
   `_parse_c2_recovery` + `conf/cdda2img.toml.example`), so a C2-capable drive uses the
   single-pass c2read path by default and cdrdao read-cd/read-toc is retired from the
   default read path (cdrdao keeps burning + the full-disc read fallback when cdrdao is
   still selected). Update CLAUDE.md rip-pipeline §1 (which currently says "Normal
   (c2_recovery=off, default)"), the man page, and any status text. Run full tests +
   a live `rip` to confirm. **Evidence backing the flip (soak, 2026-07-05):** two
   virtual CDEmu discs of different geometry (Tracy 11-trk, Under My Skin 13-trk) both
   `toc_parity` ALL MATCH — every geometry/pregap/ISRC/MCN/flag field identical to
   cdrdao read-toc; the only CD-Text-title diffs are cdrdao's own UTF-8→Latin-1
   mojibake (c2read is correct, `6e175d8`).
2. **Pre-emphasis virtual test disc.** Author a cdrdao TOC with `PRE_EMPHASIS` on a
   couple of tracks (reuse an extracted TOC+BIN, e.g. under `/tmp/cdda2img_mnt_*`),
   mount via CDEmu, run `toc_parity` — cdrdao read-toc and c2read/subq must both report
   the flag and agree. Verified feasible from libmirage source: TOC parser sets
   `MIRAGE_TRACK_FLAG_PREEMPHASIS` (parser.c:669) → `get_ctl` CONTROL bit 0
   (track.c:402) → `_generate_q_subchannel` writes `(ctl<<4)|ADR` (sector.c:1815), so
   the mounted disc carries pre-emphasis in the program-area Q. No burner/media needed;
   physical CD-RW burn via `cdrdao write` is the optional authenticity fallback.

### C2-path read progress into the TUI (2026-07-04) — c2read half ✅ DONE 2026-07-04

The c2read half is wired (Phase 1 of the c2read upgrade plan): c2read emits machine-parseable
`progress <done> <total>` lines on stdout; `c2_reader.read_disc_c2(progress_cb=…)` streams
them (stderr goes to a temp file so `hard <lba>` floods can't deadlock the reader) and
`_rip_disc_stage` feeds the TUI a real fraction. Remaining: `cdrdao_ripper.read_toc_metadata`
(cdrdao read-toc) still shows a throbber — it disappears entirely with Phase 4 of the upgrade
plan (single-pass capture drops the read-toc pass), so it is not worth instrumenting.

### Extend c2read to read the subchannel — read-only cdrdao replacement (2026-07-04) — IN PROGRESS

Planned in full: **`docs/reference/c2read-upgrade-plan.md`** (features F1–F11, difficulty
ranking, dependency build order, reference audit, per-feature strategies; §7 cdrtools
review — adopted the Plextor Q-Check C1/C2/CU census + mode-page-01 retry tuning, dismissed
-edc-corr as data-sector-only). **Phases 1–4 landed 2026-07-04**:

- **Phase 1** — single-pass audio+C2+subchannel capture (`--sub raw|q` + `--subf`),
  zero-fill of hard-unreadable sectors (PCM zeros + C2 all-ones = pure erasures
  downstream), 5-combo `--features` probe, stdout progress → TUI. PX-716A: all five 0xBE
  combos supported, field order audio→C2→sub.
- **Phase 2** — Q-stream policy layer in `subchannel.py`: `derive_track_layout`
  (pre-gaps/INDEX/CONTROL, position-slip defence) + MCN/ISRC majority voting. Pre-gaps
  match `cdrdao read-toc` frame-exactly on the live disc (all 11 tracks).
- **Phase 3** — `--fulltoc`/`--cdtext` raw dumps + `cdtext.py` decoder +
  `parse_fulltoc`/`session1_audio_tracks` (Enhanced-CD exclusion, mixed-mode refusal).
- **Phase 4** — `subq_toc.build_rip_info` assembly + pipeline switch: the C2 rip path is
  now ONE c2read pass (rip_type `c2read`; `cdrdao read-toc` fallback only if assembly
  fails); `tools/toc_parity.py` harness reports ALL MATCH vs a fresh read-toc; full live
  rip validated end-to-end (track-8 CTDB repair regression intact, 11/11 AR conf 200,
  `test` 37/37; PROV `toc_source=subq@c2read` + ISRC vote keys). rbi_spec §6.1.10: the
  embedded TOC now round-trips per-track `COPY`/`PRE_EMPHASIS` flags and INDEX ≥ 02 lines.

- **Phase 5 (2026-07-05)** — F8: per-sector retry ladder (`--retries`, default 2) with
  cache-defeat seek-away reads between attempts + sense-key classification in the summary;
  `--recovery ERR,RETR` mode-page-01 experiment flag (O_RDWR per-flag, saved page restored
  on every exit path; set/restore verified live — drive default err=0x00 retries=10).
  F10: `--speed-report` (page 2A at cdrdao's offsets — kB/s-identical to `cdrdao
  drive-info` at 4X and 40X); `drive_speed.read_drive_speed` now prefers c2read with
  cdrdao fallback. F11: `--cxscan` Plextor Q-Check census + `tools/cx_census.py`:
  first run put ALL 256 CU errors + every hotspot in track 8's known defect span
  (LBA 112500–112950), and exposed thousands of stage-2 C2 corrections in tracks
  2/7/9/11 that AR (conf 200) cannot see — the disc-health early-warning case proven.

Remaining follow-ups: the **default-flip** — ✅ USER-AUTHORIZED 2026-07-05 after the
multi-disc `toc_parity.py` soak went green (see the NEXT SESSION block at the top of
this file for the execution steps + evidence). ~~Run the `--recovery 0x20,1` experiment~~ — DONE
2026-07-05 (`tools/modepage_experiment.py`): drive-side error-recovery tuning is inert
for the miscorrection defect class (identical latency / C2 honesty / AR rate across
default, 0x20:1, 0x00:1) — rejected for adoption, flag kept as manual diagnostic; the
run also pinned the C2/audio alignment (bitmap lags audio by 2 pairs; production
erasure feed verified correct) — full verdict in `docs/reference/RECOVERY.md` §4.6.
Also observed 2026-07-04: per-rip Q valid-frame counts vary widely (157k vs 72k usable
frames on the same disc) — the floors/TOC-authority absorb it, but worth watching.

### c2read multi-pass speed-ladder recovery — ✅ TESTED + SHIPPED 2026-07-05

Recovery-strategy model (settled with the user): **c2+ctdb repair and multi-pass
re-reading are alternative exits tried in cost order** — ctanalyse±C2 first (zero extra
reads), multi-pass only when that can't fire (drive without C2, disc not in CTDB) or
fails. Both are proven; c2+ctdb is faster but doubly conditional, so multi-pass is the
**unconditional fallback we must keep** — and per the paranoia_recovery_test evidence,
the recovery mechanism is the *sweep across passes × speeds*, not cd-paranoia's engine.

**The test (run 2026-07-05, `tools/c2read_recovery_test.py`)**: c2read whole-track
re-reads across the probed ladder, multiple sweeps, **AR the only gate** (no C2, no
CTDB), on the damaged reference disc. Result: **3/3 sequential recoveries** (attempts
7, 2, 10 of a 20-attempt budget; winning speeds 32X/32X/4X — no consistent speed, the
sweep is the mechanism) + 3/3 in Thursday's c2timing baseline arm = **6/6**, every
recovery byte-identical (same AR v1) at conf 200. The harness was validated offline
first against the saved whole-disc capture (replay slicing, edge-pad paths).

**Shipped the consequence**: `_recover_failed_tracks` now re-reads via
`c2_reader.read_span` (new) in the **raw offset domain** — window read with margin
sectors, offset-corrected slice AR-verified, **verified corrected bytes spliced
sample-exactly** at `track_start*2352 + read_offset*4` (neighbouring tracks provably
unperturbed). The CTDB-failure early `apply_offset` and the cd-paranoia
boundary-disagreement full-disc re-rip bail are gone (c2read has no independent
geometry opinion); `apply_offset` now runs exactly once, at storage, on every raw path.
rip_type gains `+c2rec`. Live-validated by driving the new function against a copy of
the saved damaged capture: 3 failed tracks (2, 7, 8) all recovered on the real drive,
whole-disc re-verify 11/11 conf 200. cd-paranoia's only remaining role is the full-disc
*read* fallback when cdrdao fails (plus `track_preview`).

### PLANNED — c2read intra-read verification + boundary overlap checking (2026-07-05)

A lightweight paranoia-style self-verification layer for c2read: overlap the chunk
boundaries and cross-check the overlap regions between consecutive reads (slip/jitter
detection without any external database), optionally N-pass per-sector majority-vote
consensus as the arbiter for discs in **neither** AR nor CTDB — the one niche where
cd-paranoia's overlap verification is currently the only slip defence (the C2 experiment
proved slips are invisible to C2: coherent wrong audio, zero flags). Deliberately
**debatable and unscheduled**: justifying it requires a disc that defeats every existing
method (AR, CTDB±C2, multi-pass ladder); C2 flags could weight the consensus vote when
present. Marked as planned per user decision 2026-07-05; do not build speculatively.

### MCN archival-only + barcode as the disambiguation key (2026-06-29) — ✅ DONE 2026-06-30

Closed the Tracy Chapman live bug (an exact MB disc-ID match discarded because an archival
on-disc MCN was cross-namespace-compared against catalogued barcodes). Decided rationale +
clone-vs-archive principle in `docs/reference/identifier_trust_model.md` §1a; memory
`project_mcn_barcode_resolution`. Shipped in three commits:

- **Phase 1 (8ef6c2b)** — removed the `_is_consistent` MCN veto (ISRC veto stays).
- **Phase 2 step 2+4 (9116c20)** — `RBIDisc.barcode` (PROV-only, no format bump) + resolver
  `Field.BARCODE` + `_finalize_identifiers` (MCN synthesised from the normalised barcode,
  `mcn_source=disc|barcode_derived`). On-disc MCN no longer seeds a lookup (no carve-out).
- **Phase 2 step 5** — tore out the residual MCN disambiguation (`_disambiguate_by_mcn`, the
  §10.3 `mcn` rung, the `mcn_hits` narrowing, `barcode.mcn_matches`). Release selection rests
  on `barcode_plurality` (same-namespace). §1a end-state reached.

History: memories `project_mcn_gate_fix_decision` + `project_learned_trust_model`.

### Structural — consolidate recurring RBIDisc / MBID defect classes (2026-06-17) — FOR DISCUSSION

Two defect *classes* have each been fixed at multiple independent call sites across
separate audits — strong evidence the current pattern invites reintroduction at
every new site, rather than being a set of isolated one-off bugs. Proposed
structural fix (design before code):

1. **Hand-rebuilt `RBIDisc` drops physical fields.** Constructing a fresh `RBIDisc`
   field-by-field silently resets physical disc properties — `pre_emphasis` (the R14
   ≤1986 year-cap signal), and arguably `discogs_release_id` — to their defaults.
   Sites fixed so far: **C1** (`_merge_into_disc` / `_overwrite_disc`, `mb_lookup.py`)
   and **BUG-5** (`_clear_disc`, `metadata_menu.py`) — the same defect, two audits
   apart. Fix: a single canonical helper that merges/clears *metadata* via
   `dataclasses.replace`, preserving physical fields by construction, so no call site
   can drop them.

2. **Recording-level `mb_release_id` leaks as authoritative.** Sources that identify
   a *recording* (AcoustID, ISRC tally, duration match) must not bake a pressing-level
   `mb_release_id` into `disc.mb_release_id` as if it were disc-ID-proven. Sites fixed
   so far: **C2** (`_resolve_via_isrc_tally`) and **BUG-7** (stage-7 duration matcher).
   Fix: a single "strip pressing MBID" chokepoint on the non-disc-ID merge path (keep
   `mb_release_group_id`) so the invariant holds everywhere.

Discuss: a typed wrapper / dedicated merge API, vs. a documented chokepoint + an
invariant test asserted at each known site. Decide scope before implementing.

**LANDED 2026-06-21 (B1) — chokepoint + invariant tests chosen over the typed API.**
- **C2:** a single `mb_lookup.strip_pressing_mbid(meta)` chokepoint nulls the
  pressing-level `mb_release_id` (keeps `mb_release_group_id`); both non-disc-ID merge
  paths route through it (`_resolve_via_isrc_tally`, and the stage-7 duration-match call
  site in `cdda2img.py`). AcoustID needs no change — it corroborates, never merges.
- **C1:** the merge/clear sites already use `dataclasses.replace`; `tests/test_merge_invariants.py`
  now asserts `_merge_into_disc` / `_overwrite_disc` / `_clear_disc` preserve the
  physical/derived fields (`pre_emphasis`, `low_dynamic_range`, `cdtext_catalog_ref`,
  disc layout, track timing) — the regression guard against a future hand-built site.
- The **typed proposal schema** (the stronger enforcement) remains the deferred OPT-4
  option (B4); it only fully pays off once the collect→resolve resolver exists, which is
  not being built. See trust_model_design.md §9 and the recap below.

**Unified with OPT-4 in `docs/reference/trust_model_design.md` (2026-06-17)** — the
collect→resolve trust model would close C1/C2 by construction (physical fields proposed
at `OBJECTIVE` by one producer; recording-level sources' proposal schema omits disc-level
`mb_release_id`). Decision §5.2 there is this item's "typed API vs. chokepoint + invariant
test" choice — **resolved in favour of the chokepoint (B1, above)**.

### Remaining metadata-pipeline work (2026-06-15)

Sources: bug-hunter `private/bugs/2026-06-15_163056_metadata-pipeline.md`,
optimiser `private/optimiser/2026-06-15_metadata-consensus.md`.
BUG-1..7, OPT-1/2/3 and the superseded P3 are complete — archived in the DONE
log below.

- [~] **OPT-4** · **Per-field trust score model** — The collect→resolve resolver this item
      called for was **built and shipped as B-1…B-6** (`resolver_adapter.py`; the resolver is
      the sole Layer-2 committer in `_run_metadata_lookups` since B-4; Discogs>MB catalogue
      inversion in B-6). The original fill-blank / first-writer-wins defect is closed for the
      rip/import path. **Design + build log:** `docs/reference/trust_model_design.md` §11.
      What remains are the post-B-6 follow-ups below (no longer "awaiting a scope decision").

#### Trust-model (B4) — outstanding follow-ups (post B-6, 2026-06-27)

- [ ] **B-7** · **Menu alternatives UI** — the confirmed destination for the whole design.
      Surface `Resolution.alternatives` (cross-source losers) in the interactive metadata
      menu so the user can pick a runner-up per field; feed the structured `Resolution` into
      `build_match_distance` to **replace the current PROV string-sniffing**. The §3.1
      seed/merge decouple falls out for free. Also in scope (deferred from B-5): per-field /
      per-track direct edits, `_clear_disc`, and revert modelled as `MANUAL` proposals once
      the accumulator is carried into the menu (a persistent menu accumulator — none today;
      B-5 re-resolves `ctl.disc` on each apply). trust_model_design.md §11.4 (B-7), §9.
- [~] **B-7 prerequisite — §11.5 traceability** — *`contenders` was already shipped;
      `Resolution.skipped[key] -> ((proposal, reason), …)` landed 2026-07-29.* A value
      that was DROPPED used to appear in neither `contenders` nor `alternatives` —
      indistinguishable from one never proposed, though the two call for opposite
      fixes (mend the filter vs. mend the source). `_skip_reason` returns a reason
      rather than a boolean for exactly that. **Remaining:** the invalid-ISRC and
      "Unknown Artist" sentinel drops happen upstream of `resolve`, at the adapter,
      so routing those through the same map needs the adapter to propose-then-skip
      instead of filtering first. Original item:
- [ ] **(original) B-7 prerequisite — §11.5 traceability** · expose per-field decision provenance the
      resolver already retains internally: `Resolution.contenders[key] -> tuple[FieldProposal, …]`
      (full contender set, trust-desc) and `Resolution.skipped[key] -> reason` (empty value /
      "Unknown Artist" sentinel / invalid-ISRC drop) so a *silently dropped correct value*
      becomes visible. Answers the user requirement "if both B5 choices are wrong, which item
      in the scoring caused it?" trust_model_design.md §11.5.
- [ ] **B-4 post-soak cleanup** · delete the **retained** legacy per-source merge chain
      (`_merge_into_disc` fold), now demoted to three roles: mid-pipeline search context
      (Discogs album-match + stage-7 seed), the never-fail fallback, and the live equivalence
      **oracle** for `test_shadow_equivalence`. When deleted: that test retires and the golden
      `test_parallel_pre_menu` / B0 `test_merge_characterization` carry the guard (a conscious
      step, not silent erosion). Requires first decoupling the two consumers that read merged
      state mid-pipeline. trust_model_design.md §11.4 (B-4 notes).

---

### Minor / pre-existing

- **AcoustID `_chain_to_mb` reads an empty release-group stub.** *(2026-07-29: deferral
  unchanged — the fix is one extra request per row, the cost declined for Trk, and the
  full-release fetch on select recovers both fields. The CODE now states the
  impossibility at the read site, because "populated from a stub that is always empty"
  is indistinguishable from "populated" at the call site.)* On the recording
  endpoint, `inc=releases` does *not* embed the release-group's fields, so
  `rg.get("id")` and `rg.get("first-release-date")` are always None on the AcoustID
  path — `mb_release_group_id` and `original_release_date` have never populated there.
  (Confirmed live 2026-06-09 while fixing the invalid-include regression.) Low impact:
  the full-release fetch on select recovers this via the release endpoint. Fix would
  need a per-release follow-up call — deferred (same per-row cost we declined for Trk).

  > **SUPERSEDED 2026-08-05 — promoted to N3, and it was never "minor".** Two claims
  > in the note above are wrong, measured live. (1) **"Low impact"**: this same call
  > also truncates to **25 of 43** releases, silently dropping the disc's own release,
  > which is why `acoustid_corroborates=NO` on every container — a false negative
  > costing `+0.25` and a confidence bucket. (2) **"Fix would need a per-release
  > follow-up call"**: a paged `browse_releases(recording=…, includes=["release-groups","media"])`
  > fixes the truncation *and* the empty stub *and* keeps `medium-list` in **one**
  > paged call, not one per row — so the cost argument that justified the deferral
  > does not apply to the fix that works. The 0/N stub observation was accurate and
  > well documented; the impact assessment attached to it was inferred and wrong.
  > Lesson worth keeping: a defect logged as cosmetic gets re-read as settled, and
  > the note's own confidence is what stopped anyone measuring the second symptom.

---

## ✅ DONE — Metadata-pipeline audit BUG-1..7 + OPT-1/2/3, follow-ups, and 2026-05/06 priorities (archived 2026-06-17)

Relocated from `## Open` 2026-06-17 — all items complete; retained for reference.

### Agent audit — metadata pipeline (2026-06-15)

Sources:
- bug-hunter: `private/bugs/2026-06-15_163056_metadata-pipeline.md`
- optimiser: `private/optimiser/2026-06-15_metadata-consensus.md`

#### Bug fixes

**All BUG-1..7 DONE 2026-06-17 (commit `d5d055e`)** — fixes + regression tests
landed (`make check` + py3.10 green). Detail retained below for reference.

- [x] **BUG-1** · MEDIUM — `cddb.py:228` — `nsecs` in the `cddb query` command is computed as
      `(disc_last_lsn - track_lsns[0] + 1) // 75` (subtract-then-floor, omits lead-in), producing
      a value ~3 s short of the correct absolute lead-out in seconds that every reference client
      emits (`(disc_last_lsn + 1 + 150) // 75`). Worked example from the module's own comment:
      should be 3608, emits 3605. Exact disc-ID matches still land; impact is the gnudb fuzzy path
      and any server that re-derives the ID from offsets + nsecs.
      Fix: compute `total_secs = (disc_last_lsn + 1 + _LEAD_IN) // 75` in `query_cddb`.
      Add a regression test pinning the existing worked example.

- [x] **BUG-2** · MEDIUM — `metadata.py:66` — `derive_album_info` album fallback reads
      `Path.cwd().name` instead of the audio files' parent directory name. Docstring promises
      "parent directory name"; for `cdda2img create /music/Album` run from `/home/user`, the
      fallback is "user", not "Album". With `--auto` the wrong name is written silently.
      Fix: `tracks[0].parent.name if tracks else Path.cwd().name`.

- [x] **BUG-3** · MEDIUM — `cdda2img.py:1334-1344` — R6 AcoustID corroboration flag picks
      `consistent_rids[0]` from a list derived from a `set` (nondeterministic order). When
      AcoustID converges on more than one consistent release (e.g., the disc release plus a
      compilation sharing the same recordings), `consistent_rids[0]` is arbitrary; if the disc's
      `mb_release_id` is in the list but not at index 0, the flag is "NO" even though AcoustID
      corroborates it. Feeds the +0.25 match-confidence signal.
      Fix: `"YES" if disc.mb_release_id in consistent_rids else "NO"`.

- [x] **BUG-4** · MEDIUM — `acoustid_lookup.py:135` — AcoustID-sourced ISRCs (`_chain_to_mb`)
      bypass `validators.validate_isrc`. The merge sites (`_merge_into_disc`, `_overwrite_disc`)
      validate only the disc-side ISRC and trust that `meta.isrc` was validated at MB ingress —
      true for `_parse_release` but not for the AcoustID path, which constructs `DiscMeta`
      directly. On multi-track discs the menu's fetch-full re-parses through the validated
      `_parse_release`, closing the gap. On **single-track discs** the `DiscMeta` is applied
      directly; a malformed ISRC can reach `RBITocEntry.isrc` and the TOC `ISRC` line.
      Fix: call `validate_isrc` on the recording ISRC inside `_chain_to_mb`, or add meta-side
      validation at the merge sites alongside the existing disc-side check.

- [x] **BUG-5** · LOW — `metadata_menu.py:489-496` — `_clear_disc` reconstructs `RBIDisc` by
      hand, silently dropping `pre_emphasis` (the physical R14 year-cap signal) to `None`.
      Clearing metadata should not reset physical disc properties.
      Fix: use `dataclasses.replace(disc, album="", artist="", catalog=None, disc_id=None,
      tracks=cleared_tracks, ...)` so only metadata fields are cleared and physical fields
      (`pre_emphasis`) are preserved.

- [x] **BUG-6** · LOW — `config.py` — `Config.embedart` is declared but `load_config()` never
      reads it from the TOML data dict or passes it to the `Config(...)` constructor, so
      `embedart = true` in the user's config file has no effect.
      Fix: add `embedart = bool(data.get("embedart", False))` and include it in the constructor,
      mirroring how `auto` is handled (line 346 / line 377).

- [x] **BUG-7** · LOW — `cdda2img.py:1580-1586` — the stage-7 duration matcher returns a
      `DiscMeta` with `mb_release_id` set (to the text+duration-matched release), and
      `_merge_into_disc` writes it to `disc.mb_release_id`. This is a non-disc-ID, possibly-
      wrong pressing MBID baked into PROV as if authoritative. It feeds `populate_original_release`
      pre-menu. The gate mismatch (matcher ±15 s vs R3 ±2 s) makes it fail safe, but the PROV
      entry is wrong.
      Fix: strip `mb_release_id` from the stage-7 result before merging (keep
      `mb_release_group_id`), matching what the ISRC-tally fallback does (`C2` / `replace(winner,
      mb_release_id=None)`).

#### Performance / architecture

- [x] **OPT-1** · DONE 2026-06-17 (`c35c9d8`) — **In-process session cache for MB disc-ID lookups** — The Phase-1 banner
      (`_preview_worker`) and Phase-2 finalization (`prepopulate_from_mb`) both call
      `lookup_disc_id` with the same disc-ID (identical by construction after the SILENCE fix).
      Previously the R7 SQLite cache de-duplicated this; that cache is now removed. Replace with a
      process-lifetime `dict[str, list[DiscMeta]]` in `mb_lookup.py`, populated on first call and
      returned directly on repeat calls within the same process. No persistence, no TTL, no
      stale-data risk — the dict is discarded on process exit. Scope: `lookup_disc_id` only;
      separate dicts for ISRC and by-release-id lookups can follow if needed.

- [x] **OPT-2** · DONE 2026-06-17 (`c35c9d8`) — **In-process session cache for album art fetches** — `fetch_cover` in
      `album_art.py` has no caching. Phase 1 (banner, `_preview_worker:2137`) and Phase 2
      (`_finalize_import:1687`) both call it; when the pre- and post-menu MB/Discogs IDs coincide
      (the common path — strong auto-match or user accepts the guess), it re-downloads the same
      image bytes twice. Add a process-lifetime dict keyed on `CoverArt.source`
      (`caa:{entity}:{mbid}` / `discogs:{id}`) in `album_art.py`. Phase 2 returns the cached
      bytes when IDs match; only re-downloads on an actual ID change (user corrected metadata).

- [x] **OPT-3** · DONE 2026-06-17 — **CDDB vs stage-7 ordering** — Implemented option (a):
      stage-7 (`duration_match_lookup`) now merges *before* CDDB in `_run_metadata_lookups`, so a
      contested field goes to the track-count + ±15 s-duration-verified source rather than CDDB's
      unverified gnudb free text. CDDB is now the absolute lowest precedence (applied dead last,
      fill-blank). Documented tradeoff: stage-7's gate needs an album/artist seed, so a
      CDDB-only-seed disc never reaches stage-7 (accepted — the rare case, in exchange for the
      duration matcher outranking CDDB everywhere else). 2 regression tests in
      `test_parallel_pre_menu.py`; CLAUDE.md precedence note updated. Options (b)/(c) not taken —
      (a) is the minimal correct fix and preserves CDDB's value as a last-resort gap-filler.

#### P3 superseded

- [x] **P3** · ~~Extend the R7 SQLite cache to by-release-id / by-RG-id~~ — **SUPERSEDED
      2026-06-15 (commit `559b84a`)**: the entire R7 persistent cache and R10 offline mode have
      been removed (wrong trade-off — caching wrong results for 30 days, no user visibility or
      invalidation). Session-lifetime in-process caches (OPT-1, OPT-2) replace R7 for the
      legitimate within-invocation deduplication use cases. No SQLite extension required.

---

### Beets metadata comparison — follow-ups (2026-06-13)

- [x] **BEETS-4** · DONE 2026-06-14 (`0a42ed6`): ratio-based threshold `max(2, ceil(0.6 × n_isrc_tracks))` in `_disambiguate_by_isrcs`; `_ISRC_AGREE_RATIO = 0.6` constant; 4 new tests covering 3/10/20-track and zero-ISRC cases.

- [x] **BEETS-5** · DONE 2026-06-14 (`bde0e5c`): `_release_sort_key` sorts by `(date, country_pref)` before the `for release in releases:` loop in `_chain_to_mb`; `_COUNTRY_PREF = {"GB":0,"US":1,"XW":2}`; 8 new tests in `tests/test_acoustid_lookup.py`.

### Catalogue duplicate detection keys on editorial fields, not physical identity (2026-07-05)

Observed live: a `rip --auto --duplicate skip` of the Tracy Chapman disc registered a
second catalogue row despite an existing entry for the *same physical disc* (same MCN
`7559607740206`, same track durations, same post-repair AR v1 CRCs). Cause:
`catalogue._find_duplicates` requires an exact SQL match on `album AND artist AND
disc_number AND disc_total AND track_count` *before* the physical evidence is consulted,
then post-filters on `year` — and an `--auto` run's metadata guess (here confidence 0.30,
`mb_disc_id_multi`) can differ from a reviewed entry's year/strings while the disc is
identical. Editorial fields are best-guesses (the "Guess the Album" authority model);
physical identity is not. Proposed direction: match on the physical keys first (MCN +
track count + per-track durations + AR CRCs when both sides have them) and demote
album/artist/year to display/tiebreak. Until then, `--duplicate skip` under `--auto` is
unreliable as a re-rip guard.

### Catalogue duplicate-registration policy (2026-06-13)

- [x] **CAT-1** · Add `duplicate_catalogue_entry` config knob (values: `skip` / `replace` / `add`;
      default `skip`). When `enable_catalogue = true` and an RBI is registered, the catalogue code
      must decide what to do when a row matching the same disc already exists. "Duplicate" should be
      defined by a deterministic key — candidate: `(mb_release_id, mcn)` with fallback to
      `(album_casefold, artist_casefold)` when both identifiers are absent.
      - `skip` — silently drop the registration if a matching row exists (current implicit behaviour)
      - `replace` — overwrite the existing row (useful after a re-rip with better metadata)
      - `add` — always insert, allowing multiple RBIs for the same disc (e.g., different pressings)
      Implementation: `config.py` (`Config.duplicate_catalogue_entry: str = "skip"`); logic in
      `catalogue.py` at the registration call site; `conf/cdda2img.toml.example` entry with comment.
      Also add a `--duplicate {skip,replace,add}` CLI flag (rip / import / create) that overrides
      the config knob for that one invocation — useful for `rip --duplicate replace` after a re-rip.

- [x] **CAT-2** · Catalogue `delete` input: accept comma-separated entry numbers in addition to the
      existing `N-M` range syntax, and allow combinations (e.g. `1,3`, `2-4,7`, `1,3-5,8`).
      Implementation: a small parser in `catalogue.py` (or `catalogue_menu.py`) that splits on `,`,
      resolves each token as either a single integer or an `N-M` range, unions the resulting sets,
      and validates all indices before deleting any. Input `"1,3"` must not delete entry 2; mixed
      `"1,3-5"` must expand to `{1,3,4,5}`. Error message on invalid token (non-integer, reversed
      range, out-of-bounds index).

### Rip-to-tracks convenience pipeline (2026-06-13)

- [x] **RIP-1** · DONE 2026-06-14: `--extract` + `--no-keep-rbi` flags on `rip`; `_finalize_import` returns `Path`; `rip_image` captures it and calls `extract_image(tracks=True, embedart=cfg.embedart)` post-finally; man page updated.

### Album art follow-ups (2026-06-13)

- [x] **ART-1** · DONE 2026-06-14 (`762d0d6`): `tests/test_album_art.py` (13 tests); ART block round-trip in `TestArtBlockRoundtrip`.
- [x] **ART-2** · DONE 2026-06-14 (`762d0d6`): `embedart: bool = False` in `Config`; wired as `args.embedart or cfg.embedart` in extract CLI; example key added to `conf/cdda2img.toml.example`.
- [x] **ART-3** · DONE 2026-06-14 (`762d0d6`): `tools/albumart.py` replaced with 9-line deprecation shim (`raise SystemExit(msg)`).

---

### ⭐ Priority #1 — Agent-audit remediation (2026-05-31)

**STATUS: COMPLETE — audited 2026-06-17.** All units S/C/P/Q landed. The two non-`[x]`
items are closed by design, not pending: **C3** was reverted (`e9866eb`, do not redo —
the `discids` include makes `/discid` return HTTP 400); **P3** is moot (the R7 cache it
would extend was removed entirely in `559b84a`; OPT-1/OPT-2 replace it). Safe to archive.

Single plan covering **every** issue raised by the four background agents run on
2026-05-31 (bug-hunter, optimisation-advisor, guardian-security, flow-doc), across
security / correctness / performance / clarity. Sources:
- Guardian (signed): `private/guardian/guardian_report_20260531_135806.md`
- bug-hunter: `private/bugs/2026-05-31_092554_mb-lookup-original-release.md`
- optimiser: `private/optimiser/2026-05-31T09-26-29_mb-lookup-original-release.md`
- flow-doc: `docs/flow/{mb-lookup,original-release}.md`

Organised into independently-committable **units**; every `- [ ]` is a resume
checkpoint (run `make check` + tests + py3.10 at each). Do units in order
**S → C → P → Q** (security first; the correctness fixes sit directly on last night's
`mb_release_id` invariant work). Commit per unit so the plan survives interruption.

**Unit S — Security (HIGH; do first)**
- [x] **S1** · `toc.py:128` — make the track-title TITLE line injection-safe (GRD-…-01).
      **NOT a one-liner** — investigated 2026-05-31, the naive "wrap in `sanitize_title()`" is
      wrong twice:
      1. `sanitize_title` (toc.py:24) converts `"`→`'` but does NOT strip ASCII control chars,
         so `\n`/`\r` survive and still break out of the `TITLE "…"` line. album/artist/performer
         already use `sanitize_title` and therefore share this latent newline gap.
      2. `sanitize_title` strips ALL non-ASCII, which would REGRESS the `TRACK_TITLE_UNICODE`
         feature (toc.py:115-119): when `raw_title == track.title` no recovery comment is emitted,
         so sanitizing the TITLE line there would silently lose a Unicode title.
      Correct design: (a) add control-char stripping (`[\x00-\x1f\x7f]`) to the sanitization path
      so the newline class is closed for album/artist/performer too; (b) give the track-title
      TITLE line an injection-safe-BUT-Unicode-preserving transform (`"`→`'` + strip control
      chars, KEEP non-ASCII) — likely a new `escape_toc_string()` helper, with `sanitize_title`
      delegating to it for the control-char + quote handling. Regression test: a title with `"`
      + newline cannot inject TOC directives, AND a non-ASCII title is preserved (not stripped).
- [x] **S2a** · Spec-first (spec-before-code): define a PROV value-escaping scheme in
      `docs/reference/rbi_spec.md` §6.3 — escape `\n`/`\r` (and decide `=` handling) in values.
- [x] **S2b** · Implement symmetric escape in `build_prov_block` (`container.py:135`) + unescape
      in `_parse_provenance` (GRD-…-02). Regression test: a newline-bearing
      `original_release_title` round-trips without forging a standalone `mb_release_id=` line.
- [x] **S3** · (LOW) `toc.py:121` ISRC written raw — already mitigated by `validate_isrc`;
      confirm + add a defensive test, or fold into S1.

**Unit C — Correctness**
- [x] **C1** · F-001 — `_merge_into_disc` / `_overwrite_disc` (`mb_lookup.py`) rebuild `RBIDisc`
      by hand and drop `pre_emphasis` (+ `discogs_release_id` in overwrite) → the R14 ≤1986 cap
      is dead after any merge. Use `dataclasses.replace`. Test: merged disc retains `pre_emphasis`.
- [x] **C2** · F-002 — `_resolve_via_isrc_tally` sets a *recording-level* `mb_release_id` (the
      proven sibling of last night's AcoustID fix). `replace(winner, mb_release_id=None)`; keep
      the RG. Test: the zero-disc-ID-match path leaves `mb_release_id` None.
- [~] **C3** · F-003 — ~~add `"discids"` to the `get_releases_by_discid` includes~~ **REVERTED
      2026-06-06 (commit `e9866eb`)**. The `/discid` endpoint rejects the `discids` include with
      HTTP 400, which was swallowed as "no match" → every disc-ID lookup silently failed → CDDB
      fallback. The medium's disc-list is populated by `/discid` *anyway* (we query *by* disc id),
      so `_find_disc_medium` still selects the right medium without the include. C3 was a
      well-intentioned mistake; do not re-add `discids` to the by-discid call (whipper omits it too).
- [x] **C4** · (LOW) F-007 — guard `compute_disc_id` against >99 tracks / negative offsets. Test.

**Unit P — Performance**
- [x] **P1** · Thread `mb_result.meta` (already parsed by `prepopulate_from_mb`) into
      `original_release` so `_verify_rg_path_for_disc` + `_fetch_release_group` stop re-fetching
      the same release/RG → **3 MB round-trips/disc → 2**. Precondition (already safe): the
      re-fetch only fires when `mb_release_id` is set = a real disc-ID match = in-hand meta valid.
      Verify the R3 four-gate verify still passes against the passed-in meta.
- [x] **P2** · Remove dead helpers `_best_fuzzy_match` and the tuple-returning
      `_gather_artist_catalogue_via_mb` (reachable only from tests + `tools/demo_title_fuzz.py`);
      update those call sites.
- [ ] **P3** · (optional / **DEFERRED 2026-06-06**) Extend the R7 cache to by-release-id / by-RG-id
      lookups. Surveyed: the cache is uniformly `key → DiscMeta[]` (4 tables). `get_release_by_id`
      (`mb_lookup.py:416`) returns a `DiscMeta` and would fit a 5th table cleanly; but
      `get_release_group_by_id` (`original_release.py:247`, `mb_lookup.py:496`) returns a
      release-group shape (list of releases + dates) that needs a new serialiser/table — extra
      surface against the cache's "one shape, fail-safe" design, for an explicitly-optional item.
      User chose to defer (2026-06-06). Revisit only if MB round-trips become a measured cost; the
      by-release-id slice is the tractable first step if so.

**Unit Q — Clarity (mostly comments/decisions; fold into the touching unit where possible)**
- [x] **Q1** · F-005 — resolve the dead `_R3_PER_TRACK_TOLERANCE_MS`: wire the intended per-track
      gate, or delete the constant. (Decide alongside the C-unit.)
- [x] **Q2** · Document (code comment) why the agreed-facts multi-match path's track-count gate is
      intentionally unreachable — the RG is plurality-corroborated and the year is a group-level
      fact; only the pressing is left undetermined (by design).
- [x] **Q3** · ISRC-before-barcode ordering in the multi-match resolver: add a deliberate-decision
      comment (strict-unique ISRC winner makes it safe), or reorder to try the pressing-level
      barcode first.

### ⭐ Priority #2 — Disc-test findings (2026-05-31, investigate tomorrow)

**STATUS: COMPLETE — audited 2026-06-17.** P2-A fixed (`12f3ebc`); P2-B resolved
(folded into the #3-a plan, Units M/G/A). Both `[x]`. Safe to archive.

Surfaced by a real-disc rip (Green Day — *American Idiot*, original 2004 commercial
pressing). Both are the "a null/blank/odd value blamed on 'no record' is actually a bad
calculation" pattern — now hit 3× (R3 duration field, AcoustID pressing, and these).

- [x] **P2-A** · **AccurateRip v2 confidence always None — FIXED `12f3ebc` (2026-05-31).** Root
      cause was matching the computed v2 against the `crc450` field instead of the stored `crc`
      field; the dBAR per-track format carries a single `crc` (v1 *or* v2 per submitter) plus a
      `crc450` sub-CRC, not separate v1/v2 slots. This checkbox was stale; verified done
      2026-06-06. (Original note retained below for history.) Every track
      showed v1 matched at high confidence (127 / 128) but **v2 = `[ — ]`** (no match). v2 is
      just a different checksum of the same audio, so a v1 match at conf 127 should almost
      always have a corresponding v2 block. All-tracks-None on v2 is not credible as a genuine
      DB miss. The new dual-confidence display (DONE 2026-05-31) is what exposed it — keep the
      display; investigate the data path. Suspects, in order: `accuraterip.py:_ar_checksums`
      v2 formula `v2 = (csum_lo + csum_hi) & 0xFFFFFFFF`; `_parse_dbar` per-track v2_crc read
      (struct offset `<BLL` = conf, v1, v2); the v2 match loop in `verify_rip`. Cross-check
      against ARver's reference v2 algorithm. Evidence: `rips/IN/American Idiot…rbi`.
- [x] **P2-B** · **RESOLVED 2026-06-06 — folded into the #3-a plan (Units M + G + A, all done):**
      the shared fuzzy-MCN matcher + strict-reject consistency gate + agreed-facts over the
      MCN-matched subset together exclude releases whose barcode contradicts the disc MCN, and the
      #3-b precedence rework makes MB apply before CDDB. Original investigation retained below for
      history. **(orig)** MB multi-match ignores the disc MCN/barcode → wrong release chosen. The
      disc (MCN **093624877721**, the 2004 original — confirmed on the physical media, a
      commercial pressing not a CD-R) was identified as *"American Idiot: The Ultimate American
      Idiot" (2015)* — a reissue whose barcode is **093624922315**, which does NOT match the
      disc MCN. The disc MCN should filter/down-rank MB multi-match candidates: a release whose
      barcode disagrees with the disc MCN should be excluded. R16 already captures
      `barcode_hints: [(mbid, barcode)]` in `MBPrepopResult` but they are evidently not used as
      a disambiguation filter. Add MCN-vs-barcode filtering/ranking to the R1 multi-match
      resolver (relates to Plan A **Q3** / **C** unit).
      - **Update 2026-06-01 (investigated with `tools/trace_album_live.py`) — the root cause is
        NOT what this title says.** The displayed wrong title comes from **CDDB**, not MB:
        CD-Text is blank → CDDB (retrobridge) fills first with its single mislabeled entry
        "American Idiot: The Ultimate American Idiot", and **non-blank-wins precedence** locks it
        in. MB actually returned an **11-way multi-match and chose NO winner**
        (`mb_candidate_album=None`) — it did not "pick the reissue", it contributed no album at
        all. So two distinct defects: **(i)** precedence — a weak, un-disambiguatable source
        (CDDB) outranks MB (→ Priority #3); **(ii)** the MB fallback `_build_agreed_facts_meta`
        averages over the whole plurality release-group (which *includes* reissue [6], same RG),
        so the album collapses to None. The barcode/MCN filter is still right but must run over
        the **MCN-matched subset** (the candidates with barcode 0093624877721), not the whole RG.
        Even fixed, MB cannot override the displayed title until precedence (Priority #3) changes.
      - **Hard-case caveat to document:** publishers reuse one MCN across reissues, and a
        reissue can share the master's TOC → identical MB disc-id **and** identical MCN, which
        barcode filtering cannot split. HERE the barcodes differ, so barcode filtering solves
        this case; only the genuinely-ambiguous (same TOC + same MCN) case needs a fallback (or
        stays user-confirmed in the menu).
      - **Reference — Whipper resolved it correctly:**
        - MB disc id `RwRrGdS9dYHZI8aVdRN1LDYBYps-`
        - release group `de9bf827-a9b0-348b-a7c9-556c03c3fb07`; release-track
          `9a700326-8d3d-3f47-ab3d-40eb626b4656`
        - recorded date 2004-09-20; the correct release is GB / Reprise Records, barcode-less
          here (the `Preview changes` page already proposed `American Idiot` / 2004-08-10 / GB).

### ⭐ Priority #3 — CDDB → gnudb + lookup-precedence rework (2026-06-01)

**STATUS: COMPLETE — audited 2026-06-17.** #3-a..#3-d all `[x]`, and the #3-a sub-plan
(Units M/G/A) all `[x]`. Safe to archive.

Decided after the P2-B investigation (above) and a provenance deep-dive. Diagnostic tools
committed `233fa2b` (`tools/trace_album.py` static model + `tools/trace_album_live.py` live).

**Framing — concede the ceiling first.** No automatically-readable identifier uniquely fixes
a CD-DA *release*: MCN is reused across reissues (and not even consistent within a release
group), the TOC/disc-id is reused across pressings, CD-Text is optional and often absent
(this disc had none), and **AccurateRip is keyed solely by the TOC disc-id — it has no
release axis at all** (verified: original + reissue share one `dBAR-013-001ab0ed-…` record,
69 offset groups pooled with no per-release field). The only release-unique marks (IFPI
mastering/mould SID codes, matrix/runout, printed catalogue #) are **etched in the mirror
band — visual-only, not in any data path a drive exposes.** Everything readable identifies
*content* (mastering / recording / TOC layout), which maps many-to-one onto releases. So the
honest ceiling is **"best automatic guess + user refinement"** — which is essentially the
current model. This work is a *quality* refinement of the guess, not a capability leap; do
not chase certainty the medium cannot provide.

**Changes (do in this order — sequencing matters):**
- [x] **#3-a** · **DONE 2026-06-06 — see the #3-a plan below (Units M + G + A all complete).**
      `_build_agreed_facts_meta` now runs over the MCN-matched / consistent subset, not the whole
      RG. Expanded into a full whole-record consistency gate — see the dedicated **#3-a plan**
      block below (decided 2026-06-04).
- [x] **#3-b** · **Rework "who wins and why"** (lookup precedence) — DONE `cb4bcc7` (2026-06-04).
      CDDB demoted to LOWEST precedence (CD-Text > MB > Discogs > AcoustID > CDDB) via
      `_run_metadata_lookups`; CDDB query still parallel with MB but applied last as a zero-trust
      gap-filler. Removed the old high-trust `prepopulate_from_cddb` applier. Also fixed the
      original gnudb "Artist / Title" symptom (MB titles now win). The (a) MCN check-digit
      ranking landed `32604e3` (valid-check-digit MCNs preferred, burnable invalid kept as last
      resort, never dropped).
- [x] **#3-c** · **Replaced retrobridge with gnudb** (`gnudb.gnudb.org:8880`) as the default
      `cddb_server` (`config.py`, `cddb.py` `_DEFAULT_SERVER`/`_DEFAULT_PORT`, conf example,
      docs/man, README). Live-probed (200 CDDBP OK). retrobridge *is* a MusicBrainz bridge
      (confirmed on its homepage) → strictly redundant with our own MB lookup and lossy.
      gnudb is independent legacy FreeDB data. **NB — do not majority-vote gnudb:** plurality =
      popularity, *not* provenance; gnudb is a fallback title source only, never an authority.
      Surfaced a latent bug (now fixed): freedb `TTITLE` uses "Artist / Title"; `_parse_xmcd`
      now splits it (first " / " only, medley-safe). Also added Type/Tracks columns to the MB
      results menu (interim — CD singles shown, not filtered) + `tools/disc_scan.py`.
- [x] **#3-d** · (minor hardening) **DONE 2026-06-06.** `query_cddb` now retries the whole
      session `_CONNECT_ATTEMPTS=3` times on `OSError` (cold-connect / mid-session TCP flake),
      with a `_RETRY_BACKOFF_S` pause. Session body extracted to `_query_cddb_session`. Transport
      failure is logged at WARNING and **never cached**; only the protocol-level `202` no-match
      caches `[]` — so a flake can no longer masquerade as a legitimate "disc not in DB".

#### #3-a plan — whole-record consistency gate + fuzzy MCN (decided 2026-06-04; execute next session)

**Principle (user, 2026-06-04).** MB — and *any* service — may supplement the disc only if it
is consistent with **every non-blank on-disc objective identifier** (MCN, per-track ISRC; the
TOC is already gated by the disc-ID lookup). A candidate that contradicts a non-blank identifier
is the **wrong record**: reject it and check the next; iterate until the match list is exhausted;
if none survive, **leave the fields blank** (let AcoustID, then the manual menu, fill). Blank on
either side is allowed (no constraint). Free text (album/artist/track titles) is **corroborated,
never gated** (R9 stays as-is — gating titles is the gnudb-era regression we escaped).

**Decisions (user, 2026-06-04):**
1. **Reject, don't degrade.** Any non-blank MCN mismatch **or even a single** non-blank per-track
   ISRC mismatch ⇒ discard that whole candidate. (Supersedes the earlier "degrade to agreed-facts"
   option for single matches.)
2. **MCN comparison is fuzzy substring — everywhere in the codebase.** No metadata service
   reliably stores the full 13-digit MCN (they hold GTIN-12 printed barcodes, drop the leading
   zero / check digit, or store partial records). Exact MCN equality is therefore wrong at *every*
   call site, not just the gate. ISRC comparison stays **exact** (fixed 12-char ISO-3901).
3. **Fuzzy MCN match ⇒ fill the blanks** (fill-blank merge; disc-baked gospel — MCN/ISRC/CD-Text —
   always wins; MB only fills what the disc left blank).

**Units (each independently committable; `make check` + tests + py3.10 at each):**
- [x] **M (foundation) — shared fuzzy-MCN matcher. DONE 2026-06-06.** `barcode.mcn_matches(a, b)`
      strips both to digits and returns True iff the shorter run (≥ `_MIN_MCN_SUBSTRING_DIGITS=7`)
      is a substring of the longer. Converted `mb_lookup._disambiguate_by_mcn` and
      `cdda2img._pick_canonical_mcn` onto it. Audit result: `discogs_lookup` has **no** MCN
      equality comparison — it queries Discogs server-side by the MCN string — so nothing to
      convert there. Tests in `tests/test_barcode.py` (incl. the American Idiot pair as the
      false-positive guard) + `_is_consistent` tests.
- [x] **G — consistency gate (strict reject). DONE 2026-06-06.** `mb_lookup._is_consistent(meta,
      disc)`: fuzzy-MCN mismatch or exact per-track-ISRC mismatch ⇒ False; blank either side ⇒ no
      contradiction. Pre-filter in `prepopulate_from_mb`. Distinguishes **raw-0** (disc-ID unknown
      → R4 ISRC-tally fallback, itself now gated by `_is_consistent` per advisor) from
      **filtered-to-0** (all candidates contradict → blank, NO tally). `MBPrepopResult` gained
      `rejected_inconsistent` (surfaced in PROV as `mb_rejected_inconsistent`); `match_count` now
      = *usable/consistent* matches. Note: this filtering already feeds the consistent subset into
      `_build_agreed_facts_meta`, so it does most of Unit A's plumbing (Stage 4 is just the
      field-widening). `prepopulate_from_mb` split into `_prepop_zero_match` + `_prepop_multimatch`
      to stay under C901.
- [x] **A (#3-a proper) — agreed-facts over the consistent / MCN-matched subset. DONE 2026-06-06.**
      `_prepop_multimatch` now narrows the agreed-facts population to the **positively** MCN-matched
      subset when the disc carries an MCN (a same-RG variant with a *blank* barcode passes Unit G
      vacuously but is not identity-proven → dropped once a positive subset exists; falls back to
      the full consistent set when none positively match). `_build_agreed_facts_meta` widened to
      extract **album / artist / per-track title** gated on unanimity (new `_agreed_value` +
      `_agreed_tracks` helpers); Q2 verify-skip rationale preserved (`mb_release_id` still None).
      `_merge_into_disc` is fill-blanks-only, so disc-baked CD-Text still wins.
      - **Live-verify finding:** the captured `rips/cdrdao/American Idiot.toc` has **non-blank**
        CD-Text (`"American Idiot: The Ultimate American Idiot"`) paired with the 2004 original's
        MCN — internally contradictory. Because CD-Text is gospel (top precedence, fill-blanks),
        Unit A is **correctly inert** for that disc: the displayed album is governed by CD-Text,
        not MB. `tools/trace_album_live.py` against that TOC therefore cannot demonstrate Unit A
        (CD-Text masks it). Unit A's effect is on the **blank-CD-Text degraded case** (the original
        P2-B scenario). Proven end-to-end **offline** on the real seed (real MCN/ISRCs, CD-Text
        blanked, realistic mocked MB multi-match + CDDB mislabel applied last): Unit G drops the
        contradicting reissue, Unit A drops the blank-barcode variant, agreed album resolves to
        "American Idiot", and CDDB cannot overwrite it. Tests in `tests/test_mb_lookup.py`
        (Unit A section: `_agreed_value` / `_agreed_tracks` / widening / disagreement / MCN-subset
        exclusion / no-positive-match fallback).
      - **Precedence note:** the 2026-06-01 P2-B remark "MB cannot override the displayed title
        until precedence changes" is **stale** — `cb4bcc7` demoted CDDB to lowest precedence (MB
        applies first, CDDB last as zero-trust gap-filler), confirmed live in
        `cdda2img._run_metadata_lookups`.

**Cascade note:** the gate makes MB return blank more often → AcoustID (last-resort autopopulate)
and the manual menu fire more often. That is the intended "prefer no-answer over wrong-answer"
behaviour, not a regression.

---

- **`cdda2img.barcode` → general `validation` module** — `barcode.py` is the
  single-function module carved out of `discogs_lookup.py`. If more EAN/UPC
  helpers accumulate, fold `normalize_barcode` into a broader validation
  module alongside the ISRC and GTIN-13 helpers in `validators.py`. No
  action required while it stays a one-function file.
- **Metadata-menu screen-stack port** (full scope chosen 2026-05-31) — replace the
  flat `MenuState` enum + blocking-delegate sub-menus with a **screen stack** on
  `MenuController`: each page is a `Screen` (pure `render` + one-step `handle_input`
  returning a `Push`/`Pop`/`Done`/`Stay` nav intent the controller applies); the stack
  carries per-screen context (e.g. which track, which search results). Migration
  checkpoints (each behaviour-preserving + committable):
  - [x] **1** · Scaffold — `Screen`/`Nav`, `controller.stack` + `done`, `run/_step/_apply`;
        port MAIN + AR_PAUSE; EDIT/FETCH/ORIGINAL_RELEASE bridged by `LegacyDelegateScreen`.
        Tests rewritten to drive the stack. (commit, 2026-05-31)
  - [x] **2** · EDIT → `EditScreen` + `EditTrackScreen` + `EditDiscPositionScreen`
        (native screens; MAIN now pushes `EditScreen`, EDIT no longer routed through
        `LegacyDelegateScreen`). Disc-position validation loop expressed as `Stay`;
        per-track screen carries `track_number` and re-resolves each step. +18 tests.
        (commit, 2026-06-02)
  - **3** · FETCH → Fetch + MBSearch/**MBResults** + Discogs + Acoustid. Split into a/b/c
        (advisor: a 600-line single commit is hard to bisect); each behaviour-preserving.
        Frame-vs-helper rule applied: a *frame* (Screen) is navigable/paginated/persistent;
        `_confirm_apply`/`_show_diff` stay blocking leaf helpers in `handle_input`. Persistent
        feedback ("Applied.") → `ctl.banner` (a plain print is wiped by the next screen-clear
        in TUI mode); transient IO prints ("Searching…") stay.
    - [x] **3a** · MusicBrainz. DONE 2026-06-06. `FetchScreen` (replaces
          `LegacyDelegateScreen(FETCH)`; delegates d/a to legacy `_discogs_menu`/`_acoustid_menu`
          as interim blocking leaves) + `MBSearchScreen` ("enter query"; artist/title as instance
          state seeded at entry, mutated only by [e], no drift to post-apply `disc.album`) +
          `ResultsScreen` ("pick result"; page index = screen state; pure repaint via extracted
          `metadata_menu._render_results_page`; source-discriminated apply tail). MB apply tail:
          sort earliest-first before push, fetch-full-before-preview, merge/overwrite, thread
          `mb_rg_id`. Removed legacy `_fetch_menu`/`_mb_search_menu`/`_mb_select_and_apply`;
          `_select_from_results` kept (Discogs/AcoustID/original use it) refactored onto
          `_render_results_page`. Migrated 2 tests + 13 new native tests. 810 pass.
    - [x] **3b** · Discogs. DONE 2026-06-06. `DiscogsSearchScreen` (mirrors `MBSearchScreen`;
          token-unavailable guard renders help + pops on any key, preserving legacy
          `_discogs_menu`) + `ResultsScreen(source="discogs")`. Apply tail preserves the legacy
          asymmetry vs MB: confirm runs BEFORE `fetch_release` (stub reaches the preview), no
          sort, no `mb_rg_id` threading — noted in a code comment as a deliberate carry-over.
          Removed `_discogs_menu`/`_discogs_execute_search`; FetchScreen [d] pushes native. +6
          tests. 815 pass.
    - [x] **3c** · AcoustID. DONE 2026-06-06. `AcoustidScreen` (track-picker; wavs/pcm modes;
          pcm mode lazily creates a `TemporaryDirectory` + per-track WAV cache, cleaned by the
          finalizer on pop/GC) + `AcoustidFileScreen` (file-path entry — made its own screen, not
          a blocking helper, for stack uniformity) + `ResultsScreen(source="acoustid")`. Avail
          guard moved to `FetchScreen._push_acoustid` (banner on unavailable; same wavs→pcm→file
          dispatch as legacy `_acoustid_menu`). Tagging (single-track `number=None` → track
          number) in the extracted pure `_acoustid_fingerprint`; track-list render in pure
          `_render_acoustid_tracklist`. Apply tail preserves legacy order: confirm before
          fetch-full, fetch-full when `len(tracks) < len(disc.tracks)`, no `mb_rg_id`. Results
          frame pops back to the picker (loops). Removed `_acoustid_run_one`/`_acoustid_file_loop`/
          `_acoustid_pcm_loop`/`_acoustid_wavs_loop`/`_acoustid_menu` + the orphaned `tempfile`
          import. Migrated 2 tests + 10 new. 826 pass. **cp3 (FETCH) fully native.**
  - [x] **4** · ORIGINAL_RELEASE → `OriginalReleaseScreen`. DONE 2026-06-07 (a4b9cef).
        Persistent hub (mirrors `EditScreen`): [m] set-manually / [c] clear are inline
        bounded modals → `Stay`+banner ([m] banner derived from post-call disc state, set
        vs clear); [s] fetches MB releases (rg id or prompted text via
        `_fetch_releases_for_group`), sorts earliest-first, pushes
        `ResultsScreen(source="original")`; [b] is the single exit to MAIN. `ResultsScreen`
        gains the `original` apply tail (`_confirm_original` → `_apply_selected_release`,
        threads `mb_rg_id`, pops to hub). Removed `LegacyDelegateScreen`,
        `_original_release_menu`, `_search_and_select_original`, `_select_from_results`.
        +9 native tests. 834 pass. **Whole menu now a native screen stack; no
        procedural-loop bridge remains.**
  - [x] **5** · Delete the dead legacy helpers. DONE 2026-06-07 (c6293ae). Removed
        `_edit_menu`/`_edit_disc_position`/`_edit_track` (−89 lines; referenced only each
        other once cp2 made EDIT native). Shared helpers
        `_print_disc_summary`/`_prompt_edit`/`_header` survive. Reworded 4 docstrings that
        named the deleted symbols / "state machine over `MenuState`". `MenuState` enum
        **kept** (not collapsed): all 12 members are live screen identities, controller
        `.state` reads `stack[-1].state`, ~40 tests assert on it — retiring = pure churn,
        zero behaviour change. 834 pass.
- **Suppress the duplicate AR report print in `rip_image`** — once AR_PAUSE
  is the canonical display surface, the standalone `print_ar_report` call
  in `rip_image` writes to stdout and is immediately wiped by AR_PAUSE's
  screen-clear. Cheap to keep for now (batch / non-TTY mode still needs
  the stdout copy); the refactor should route both paths through one
  helper, ideally gated on `sys.stdout.isatty()` or an explicit "batch"
  flag.
- **⭐ NEXT (chosen 2026-06-09; target this weekend) — Research `private/code/beets`** —
  analyse its metadata workflow
  (resolver chain, plugin model, ID-tagger, MB/AcoustID integration) and
  compare to cdda2img. Specifically check whether beets has a better
  approach to the multi-source merge problem that R1/R8/R9 address, and
  whether its conflict-resolution UI is worth porting. Write findings to
  `private/research/incoming/beets-comparison.md`.

## ✅ DONE — Stage 7: last-resort duration match (2026-06-08)

Final stage of the metadata-pipeline plan. A whipper-style duration matcher as
the **lowest-precedence** source, below even CDDB — surfacing a best-guess MB
release for the user to correct in the menu when nothing richer identified one
("Guess the Album" model; no authoritative ground truth exists, so no-answer is
the wrong default).

`mb_lookup.duration_match_lookup(disc)` fires only when `disc.mb_release_id is
None` and an album/artist is available to search with. It text-searches MB,
**pre-filters stubs by track count** (a stronger gross discriminator than
duration, and it slashes the per-candidate fetch-full fan-out — MB is pinned to
1 req/s), fetches the survivors full (capped at `_DURATION_MATCH_MAX_FETCH=8`),
and picks the candidate whose total duration best matches the physical disc.

Two duration conventions, anchored separately so a constant offset never sways
the `argmin` winner (only the absolute accept/reject gate):
- `track.length` (TOC-derived; **includes** the following track's pregap) →
  compared against the pregap-inclusive `RBIDisc.total_frames`.
- `recording.length` (canonical pure-audio; the rare fallback for a medium with
  no per-track length) → compared against the audio-only `sum(duration_frames)`,
  read self-contained so it never leaks into `TrackMeta.duration_ms` / the R3
  ±2 s gate (which deliberately refuses `recording.length`). The two pools are
  never mixed into one ranking; track.length is preferred whole.

Gate `_DURATION_MATCH_TOLERANCE_MS=15_000` is generous (rejects only off-by-
minutes); it's the single knob to tune from real-world testing + bug reports.
Wired as the final step of `cdda2img._run_metadata_lookups` after the CDDB
merge, via fill-blank `_merge_into_disc`. Surfaces `duration_match_release` in
PROV. `_fetch_release_raw` extracted (raw release dict retaining
`recording.length`); `lookup_release` now delegates to it. +14 tests (pure
`_sum_*`/`pick_duration_match` + mocked `duration_match_lookup` incl. track-count
pre-filter and tolerance reject). 849 pass (3.14 + 3.10); make check clean.

## ✅ DONE — disc_scan `--deep`: raw subchannel Q-channel provenance (2026-06-03)

Groundwork for the "disc is gospel" authority model (Priority #3): true lead-in
vs program-area provenance for MCN/ISRC, which the cdrdao `.toc` cannot give
(it collapses subchannel region). New pure module `src/cdda2img/subchannel.py`
decodes the Q-channel out of a redumper `.subcode` (Q = bit 6 of each subcode
byte; CRC-16/GSM; ADR 1=position/TOC, 2=MCN, 3=ISRC), anchors the file's base
LBA from program position frames (lead-in ADR=1 carries the TOC, not a
position — excluded), and attributes each MCN/ISRC to lead-in or a program
track with LBA spans. ISRC value-decode included (6-bit owner code + BCD
digits). Lead-out from the sibling `.fulltoc` (point 0xA2). Wired as
`tools/disc_scan.py --deep <subcode>` (standalone or with `--toc`/`--device`);
stable-location rows feed the cross-disc stats, a rich per-disc table shows
frame counts + LBA spans. Validated non-circularly on a PX-716A *American
Idiot* capture: MCN `0093624877721`, ISRCs `USRE104008xx` (RE1 = Reprise, the
disc's actual label), base LBA −45150 at 100% anchor agreement, program-area
invalid-Q 314 vs redumper's logged 315 (the +1 is a lead-out-overread sector
redumper counts and we exclude — a range-boundary difference, not a defect).
`src/cdda2img/subchannel.py` + `tests/test_subchannel.py` (12 tests, real
hex fixtures since `rips/` is gitignored). 704 tests (3.14 + 3.10); make check
clean. ISRC 6-bit packing + Q-error-counter semantics read from
`private/code/redumper`.

## ✅ DONE — AccurateRip v2 dual-confidence display (2026-05-31)

`format_ar_report` (`accuraterip.py`) used an `if confidence_v1 … elif confidence_v2`
chain, so when both CRC variants matched (the normal success case) the v2 branch was
unreachable and only v1's — usually lower — confidence was shown. Now each track renders
both: `Track  1: v1=76e30f97 [57]   v2=ad4a33e8 [113]  OK`, with `[ — ]` for a variant
that had no DB match and `MISMATCH (max N)` when neither matched. The footer's
"min confidence" switched from v1-first to the weakest track's *stronger* variant
(`max(v1, v2)` per track). Display-only; the persisted ARIP block is unchanged. Tests
in `tests/test_menu_state.py`.

## ✅ DONE — Release intelligence refactor: low_dynamic_range + original_release (2026-05-25)

449 tests; ruff + ty clean. Catalogue schema bumped to v2 (drop and re-scan; userbase is zero).

- [x] **Killed the `remastered` enum entirely** — `_classify_remaster`, `guess_remaster_status`,
  `REMASTERED_*` / `REMASTER_KEYWORDS` / `LOUDNESS_WAR_YEAR` constants, the `remastered_source`
  field on RBIDisc/DiscMeta, the PROV `remastered` key, the catalogue `remaster` column, the
  metadata-menu remaster classifier, and all associated tests. The four-valued guess
  (UNKNOWN/NO/POSSIBLE/YES) conflated "is this a re-mastering?" with "does this sound
  compressed?" — neither question was being answered factually. ZZ Top *Eliminator* (1983,
  LRA 3.8 LU) was the canonical counterexample: an objectively low-DR album that predates
  the loudness war by a decade.
- [x] **`low_dynamic_range: bool | None` on RBIDisc** — derived from `rg_result.album_lra <
  cfg.low_dr_threshold`. `None` when `--loudness none` was used. Threshold configurable via
  `Config.low_dr_threshold` (default 5.0 LU, range 0.5–20.0). Persisted to PROV (`YES`/`NO`)
  and the catalogue (`low_dynamic_range INTEGER`).
- [x] **`original_release_*` on RBIDisc** — `original_release_found: bool` +
  `original_release_title: str | None` + `original_release_year: int | None`. Populated by
  `original_release.py:find_original_release()` via MusicBrainz release-group lookup. Rejects
  derivative secondary types (Compilation, Live, Remix, etc.). Self-match rejected: a 1983
  album whose RG first-release-date is 1983 does not "have an earlier release". Manual override
  available via the metadata-menu `[m]` Set manually action.
- [x] **`--silence trim|notrim` replaces `--mode master|remaster`** — clearer naming; drops
  the confusion between "studio remaster" and "cdda2img's remaster mode". The existing
  `--silence N` (threshold) renamed to `--silence-threshold N` and Config field
  `silence` → `silence_threshold` to free up the `--silence` name. `--no-trim-silence`
  dropped (redundant with `--silence notrim`).
- [x] **PROV reader/display** — `Low DR:` and `Original:` lines replace the old `Remaster:`
  line in `list` output and the metadata menu summary. `RGResult.warnings` (the editorial
  "loudness war mastering" message) deleted — measurement is reported, not editorialised.
- [x] **Catalogue schema v2** — `remaster TEXT` dropped; `low_dynamic_range INTEGER`,
  `original_release_found INTEGER NOT NULL DEFAULT 0`, `original_release_title TEXT`,
  `original_release_year INTEGER` added. `_check_schema_version` hard-aborts on v1 with a
  clear "delete and re-scan" message.
- [x] **Research delivered** — `private/research/incoming/original-release-detection.md`
  (~28 KB) documents the allow-list, deny-list, MB release-group API, Discogs masters,
  fuzzy-match algorithm (rapidfuzz `token_set_ratio` @ 88), and DR-database survey. Powers
  Phase 3b (title-fuzz fallback) when picked up.

Not yet done (deliberately deferred):
- [ ] **Title-fuzz fallback for MB-miss cases** — when MB has no disc-ID hit, fuzzy-match
  against artist catalogue via Discogs/MB. Algorithm fully specified in the research file;
  requires the `rapidfuzz` dependency. Open question.

---

## ✅ DONE — Track-1 audio preview during rip (2026-05-20)

387 tests; ruff + ty clean. Verified on hardware (PX-716A).

- [x] **`track_preview.py`** (new module) — `start_preview(device, work_dir, progress_cb)`
  grabs track 1 via `cd-paranoia -Z` (fast, no paranoia — it is a throwaway preview) to a
  temp WAV, then loops it with `ffplay -loop 0` as a detached background process.
  `TrackPreview.stop()` terminates playback and deletes the WAV. Best-effort: every
  failure path (missing cd-paranoia/ffplay, grab error) is swallowed, so a rip is never
  affected. Progress is derived by polling the growing WAV size against the known track
  length — robust and tool-agnostic, unlike parsing cd-paranoia's progress display.
- [x] **`r` pipeline integration** — `rip_image()` grabs track 1 first (single optical
  drive, so the grab is sequential before the cdrdao rip), shows a real "Grabbing
  track 1…" progress bar, then plays it on a loop through the cdrdao rip, metadata menu,
  loudness analysis and container build. `ffplay` gets `stdin=DEVNULL` so it cannot steal
  keystrokes from the metadata menu. Skipped when not a TTY; stopped in the `finally` via
  `_stop_preview()`. Track 1 is read twice (cd-paranoia preview + cdrdao archive rip) —
  an accepted cosmetic cost.
- [x] **Refactors** — `disc_reader._query_disc` → public `query_disc` (reused for track
  1's length); `cdda2img._rg_progress_cb` → general `_phase_progress_cb(ui, label)`,
  shared by the loudness and "Grabbing track 1…" progress bars.
- [x] **Tests** — `tests/test_track_preview.py` (3): tools-missing → None, internal
  error → None (never raises), `stop()` terminates playback + cleans up.

---

## ✅ DONE — TUI progress bars: cdrdao rip + EBU R128 loudness (2026-05-20)

384 tests; ruff + ty clean.

- [x] **cdrdao rip progress overshoot fixed** — `cdrdao_progress.py`: cdrdao prints the
  *absolute* disc MSF position (`CdrDriver.cc:4062`/`4090`), not a track-relative offset.
  The parser was adding a per-track base on top, overshooting the leadout (observed
  220655/204143, hitting 100% at track 10 of 11). The MSF value is now used directly as
  elapsed and clamped to total; the `_done_frames`/`_track_frames` machinery is removed.
  `cdrdao_ripper.py` now reads cdrdao **stderr** (where progress text goes), not stdout.
  Confirmed working on a real rip.
- [x] **Loudness progress bar** — `replaygain.py:analyse_raw()` scans each track in
  `_RG_CHUNK_FRAMES` (750 ≈ 10 s) chunks and calls an optional `progress_cb(done, total)`;
  libebur128's incremental `add_frames()` makes chunked feeding bit-identical to one call.
  `cdda2img.py:_rg_progress_cb()` drives the TUI bar — previously an indeterminate
  "bobber". Chunking also bounds the float32 conversion buffer. Confirmed on a real rip.
- [x] **Tests** — `tests/test_cdrdao_progress.py` (5: absolute-MSF parsing, monotonic
  progress, no overshoot); `tests/test_replaygain.py` (3: progress-callback contract,
  chunk-size invariance, empty-disc guard).
- [x] **`container.py:build_container` C901 fix** — four `dir_count` counters collapsed
  into one `sum(...)` expression (the `quiet=` parameter had pushed complexity to 11).
- [x] **Docs / config** — `CLAUDE.md` corrected (ruff line length is 88, not 120);
  `album/` added to `.gitignore`; `scratch/` excluded from ruff and ty in `pyproject.toml`
  (it holds throwaway prototypes — the source of `sync.py` `ruff check .` failures and of
  stray `ty` warnings; `ruff check`/`ty`/pre-commit had three different file scopes).
- [x] **Follow-up — Python 3.10–3.13 CI fix** — f496e21 was verified only on 3.14 (the
  dev runtime) and broke the older CI matrix. `cdda2img.py` used a `TYPE_CHECKING`-only
  `TerminalUI` in unquoted annotations with no `from __future__ import annotations` —
  lazy on 3.14 (PEP 649) but eager at definition time on 3.10–3.13 → `NameError`.
  `container.py:build_prov_block` used `datetime.UTC` (added in Python 3.11) →
  `AttributeError` on 3.10. Fixed with the future import (ruff then dropped the
  now-redundant quoted annotations) and `datetime.timezone.utc`; suite verified on
  Python 3.10 and 3.14 (`uv run --python 3.10 pytest`).

---

## ✅ DONE — Metadata menu improvements + catalogue UI fixes (2026-05-15)

303 tests; ruff + ty clean.

- [x] **Remaster status in metadata summary** — `_print_disc_summary` shows
  `Remaster: YES/POSSIBLE/NO (orig. YYYY)` when `remastered_source != UNKNOWN`.
- [x] **Manual remaster entry** — `_set_remaster_manually()` added; 1–4 maps to
  YES/NO/POSSIBLE/UNKNOWN; YES triggers year prompt with inline error for non-4-digit input.
- [x] **Year-only date storage** — `original_release_date` pruned to `YYYY`.
- [x] **`[r]` menu restructured** — `_original_release_menu` now shows `[s]`/`[m]`/`[b]`
  before any MB fetch.
- [x] **Results prompt simplified** — `"Select 1-N or command:"` → `"Select 1-N:"`.
- [x] **Catalogue menu navigation fix** — blank Enter returns to summary; `_search_loop`
  returns `"summary"` vs `"quit"` sentinel.
- [x] **Year column alignment fix** — spurious spaces removed from year column.
- [x] **Output filename fix** — `_finalize_import()` recomputes `output_stem` from
  `disc.album` after `run_metadata_menu()`; `sanitize_title` moved to top-level import.
- [x] **Docs: README, LINT (LINT-015/016), TODO, man page updated for v0.1.7**;
  `d` subcommand documented in man page.

---

## ✅ DONE — RBI v4.0, ARIP/RLOG blocks, x/l refactor, embed_rg_tags fix (2026-05-14)

275 tests; ruff + ty clean.

- [x] **RBI v4.0** — 40-byte fixed header; block directory at end of file; block types:
  TOC, PROV, RGDB, ARIP, RLOG, PCM. SHA-256 per-block checksum in directory entries.
  Old 169-byte v3.0 header retired; `BLOCK_FLAG_SKIP` signals blocks safe to ignore.
  `verify_container` updated to 27 rules. `read_header` returns `RBIHeader` with
  `find_block(type_id)` helper; `build_container` writes directory after all blocks.
- [x] **ARIP block** — `accuraterip.py:pack_arip_block()` / `unpack_arip_block()`;
  stores disc IDs (id1/id2/cddb_id), per-track v1/v2 CRCs, confidence, status, and db_total
  in a compact binary format. Written by `rip_image()` after `verify_rip`; readable via
  `cdda2img l --ar` and `cdda2img x --ar`. `format_arip_text()` renders CUETools-style report.
- [x] **RLOG block** — `rip_log.py:RipLogBuilder`: structured rip log (drive name, engine
  version, read offset, per-track AR results); SHA-256 self-seal; written by `rip_image()`
  and `import_image()`. Readable via `cdda2img l --log` and `cdda2img x --log`.
- [x] **Remaster auto-guess heuristic** — `metadata_menu.py` auto-sets `remastered_source`
  to `YES`/`POSSIBLE`/`NO` from MB release title keywords and first-release-date comparison.
- [x] **x/l refactor** — `x`: `--tracks`, `--raw`, `--rg`, `--ar`, `--log`, `--all`
  (default); output to `extracted/`; `ExtractOptions` dataclass. `l`: `--info`, `--rg`,
  `--ar`, `--log`; all output to stdout (no pager).
- [x] **embed_rg_tags fix** — PyAV 16 dropped `add_stream(template=)`. Replaced PyAV
  stream-copy remux with mutagen in-place Vorbis comment patch — no audio re-encoding.
  LINT-003 resolved.
- [x] **cdrdao version probe fix** — `rip_log.py` now uses `cdrdao version` (subcommand)
  instead of `cdrdao --version` (illegal command that returned error text as version string).

---

## ✅ DONE — Write offset measurement tool (2026-05-10)

PX-716A write offset confirmed: **−30 samples** (3 cycles, 100% confidence).
Combined offset = read_offset + write_offset = +30 + (−30) = 0 (self-correcting,
same-drive round-trip). Burn correction: prepend 30 samples silence before burning.

- [x] **`tools/measure_write_offset.py`** — standalone burn-and-read-back write offset tool:
  - Generates a 75-second synthetic test signal with noise bursts at 1.0 s and 60.0 s.
  - Burns via `cdrdao write`; rips via `cdrdao read-cd`; applies read offset correction.
  - Detects pulse positions by RMS peak detection (±8820-sample search window).
  - `write_offset = found_position − expected_position` per cycle.
  - Dual-pulse internal consistency check flags defective discs.
  - Accumulates cycles in `rips/write_offset_results.toml` (atomic TOML write; resumable).
  - Sign convention: W < 0 = burns early; burn correction = prepend |W| silence to disc stream.
  - McCabe complexity kept under 10 by extracting `_run_one_cycle()`.
- [x] **`docs/research/OFFSETS.md`** (new) — documents read offsets, write offsets, combined
  offset, and cdda2img strategy; includes key facts for PX-716A.

---

## ✅ DONE — AccurateRip unit tests + numpy speedup + metadata menu bug fix (2026-05-10)

196 tests total; ruff + ty clean.

- [x] **`tests/test_accuraterip.py`** (17 new tests):
  - `_ar_disc_ids`: frozen Technotronic vector (12 tracks), lsn-zero guard, 32-bit wrap.
  - `_ar_checksums`: middle track, first/last fully excluded, multiplier 1-based, overflow
    (v2≠v1 via csum_hi), boundary inclusive, padding-differs-from-clipping invariant.
  - `_parse_dbar`: empty, two-block happy path, truncated block ignored, wrong n_tracks.
  - `verify_rip`: disc-not-in-database early return; last-track zero-padding integration
    (patches `_fetch_ar`; proves padded v1 ≠ clipped v1; verify_rip returns conf=15).
- [x] **numpy speedup — `src/cdda2img/accuraterip.py:_ar_checksums`**:
  - Rewritten with numpy: `np.frombuffer` zero-copy view → slice `[lo:sum_to]` →
    `arange(lo+1, sum_to+1, dtype=uint64)` → vectorized multiply + bitwise sum.
  - ~20× speedup: ~264 ms/track vs ~5 s/track on a 4-minute track (10.5M frames).
  - `numpy>=1.24` added as explicit dep to `pyproject.toml`.
- [x] **Bug fix — `metadata_menu.py:_original_release_menu`**:
  - `[r] Find original release` was incorrectly calling `_merge_into_disc(selected, disc)`,
    writing ISRCs and per-track metadata from the original release to the current disc.
  - Fix: removed the `lookup_release` fetch and `_merge_into_disc` call entirely.
    Now sets only two provenance fields: `disc.original_release_date` and `disc.remastered_source`.

---

## ✅ DONE — AccurateRip drive offset catalog + [[drives]] config persistence (2026-05-10)

End-to-end validated: Plextor PX-716A auto-detected at +30 samples (2781 AccurateRip
submissions), persisted to `[[drives]]` in `cdda2img.toml`, `Drive:` line shown in
`cdda2img l` output.

- [x] **`src/cdda2img/db.py`** (new module): `open_drive_offsets_db(cfg)` — WAL-mode
  SQLite at `$XDG_DATA_HOME/cdda2img/drive_offsets.db`; schema: `ar_drives` (ar_name,
  offset, submissions), `fetch_log` (cooldown tracking), `fetch_state` (Last-Modified/ETag
  cache for future use). `ensure_backup()` / `_rotate_backups()` / `parse_frequency()` for
  rotating database backups. `_apply_schema()` is idempotent (IF NOT EXISTS throughout).
- [x] **`src/cdda2img/drive_info.py`** (new module):
  - `probe_drive_name(device) -> str | None`: sysfs `/sys/block/<dev>/device/{vendor,model}`;
    collapses whitespace; returns `"VENDOR MODEL"` or `None` on OSError.
  - `_normalize_ar_name(raw)`: two-pattern approach — Pattern 1 `^-\s+(.*)` for no-vendor
    entries (`"- 16X12 DVD DUAL"` → `"16X12 DVD DUAL"`); Pattern 2 `^(.*?)\s+-\s+(.*)`
    with `\s+` on **both** sides of hyphen (distinguishes `HL-DT-ST` intra-hyphens from
    `" - "` separator).
  - `ensure_drive_offsets(conn)`: fetches `http://www.accuraterip.com/driveoffsets.htm`;
    30-day cooldown via `fetch_log`; atomic `DELETE+INSERT` into `ar_drives`; handles
    network errors (warns, no-op) and 304 (logs only). AccurateRip sends no caching headers
    so every request is a full 200 — cooldown is the sole throttle.
  - `find_drive_offset(conn, drive_name) -> tuple[int, int] | None`: highest-submissions
    match by exact name.
- [x] **`src/cdda2img/config.py`** extended:
  - `DriveConfig(name: str, offset: int)` dataclass. No `submissions` — that's an AR
    property recoverable from `ar_drives`.
  - `Config.drives: list[DriveConfig]` parsed from `[[drives]]` blocks.
  - `_toml_quote(s)` — TOML basic-string literal with `\`, `"`, `\n` escaping.
  - `_rewrite_config_drives(text, drives)` — line-walker: strips all `[[drives]]` blocks,
    appends fresh entries at EOF. Correctly handles mid-file blocks.
  - `save_drive(drive, path=None)` — upserts by name; atomic write (`.tmp` + `Path.replace()`);
    falls back to `{}` on `TOMLDecodeError`.
  - `conf/cdda2img.toml.example` updated with a commented `[[drives]]` example block.
- [x] **`src/cdda2img/cdda2img.py`** — `_resolve_drive_offset(device, cfg) → (int, str | None)`:
  resolution order: `cfg.drives` → AR catalog (auto-apply ≥3 submissions, prompt <3,
  no-op without TTY) → `cfg.drive_offset`. Persists via `save_drive()`; swallows `OSError`
  with a warning. `rip_image()` calls it before the rip; unpacks `(drive_offset, drive_name)`;
  adds `PROVENANCE_DRIVE_NAME` / `PROVENANCE_DRIVE_OFFSET` (formatted `+N`) when drive name
  is known.
- [x] **`src/cdda2img/container.py`** — `_print_provenance()` emits
  `Drive:     PLEXTOR DVDR PX-716A  (offset +30)` between `Type:` and `Remaster:` lines
  when `PROVENANCE_DRIVE_NAME` is present.
- [x] 179 tests, ruff + ty clean.
  - `tests/test_db.py` (21 tests): `parse_frequency`, backup helpers, `ensure_backup`,
    `open_drive_offsets_db` schema/WAL/idempotency.
  - `tests/test_drive_info.py` (25 tests): `probe_drive_name`, `_normalize_ar_name`,
    `_parse_drive_offsets_html`, `ensure_drive_offsets` (cooldown/stale/error/304/atomic),
    `find_drive_offset`.
  - `tests/test_config.py` (26 tests): `_toml_quote`, `_parse_drives`, `_rewrite_config_drives`,
    `save_drive` round-trips.
  - `tests/test_resolve_drive_offset.py` (10 tests): all 6 resolution paths + OSError swallow.

---

## ✅ DONE — AccurateRip verification + first-run config (2026-05-09)

End-to-end validated: Technotronic *Pump Up the Jam* (12 tracks) at conf 14/136;
Madness *Divine Madness* (22 tracks) at conf 13–14/155–166, all 22 tracks OK.

- [x] **`src/cdda2img/accuraterip.py`** (new module):
  - `ARTrackResult` dataclass: track, v1_crc, v2_crc, confidence_v1, confidence_v2, max_confidence.
  - `_ar_checksums(frames, track, total_tracks)` — AccurateRip v1/v2 checksum (pure Python).
    Multiplier 1-based from frame 0; boundary exclusion via `sum_from`/`sum_to` guards.
    `sum_from = 2940 if track == 1 else 0`; `sum_to = n-2940 if track == last else n`.
    `v1 = csum_lo & 0xFFFFFFFF`; `v2 = (csum_lo + csum_hi) & 0xFFFFFFFF`.
  - **Zero-padding invariant**: when the drive-offset read window extends past the PCM file
    boundary (positive offset: last track; negative offset: track 1), the raw buffer is
    zero-padded rather than clipped. Clipping shifts `sum_to` and mismatches the last track.
    Confirmed: track 22 mismatch on Madness disc with drive_offset=+30, fixed by padding.
  - `_ar_disc_ids(track_lsns, disc_last_lsn)` — ARver disc ID formula; inputs are LSNs.
    `id1 = sum(track_lsns) + lsn_leadout`; `id2` weighted sum + `lsn_leadout * (n+1)`.
  - `_ar_url` — directory uses the **last three chars of `id1` in reverse order** (LSBs first).
  - `_parse_dbar(data, n_tracks)` — parses binary dBAR response into per-block per-track dicts.
    Multiple blocks per disc (one per drive-offset group); `verify_rip` matches against all.
  - `verify_rip(pcm_path, track_lsns, disc_last_lsn, drive_offset=0, cddb_id=0)` — full pipeline.
    Early-returns with `max_confidence=None` results if disc not in database.
  - `print_ar_report(results, drive_offset=0)` — per-track output. When all tracks mismatch
    but the disc IS in the database, prints a concise drive_offset hint instead of N MISMATCH
    lines. Partial mismatches always show per-track output.
- [x] **`src/cdda2img/cdda2img.py`** — `rip_image()` calls `verify_rip` + `print_ar_report`
  after `prepopulate_from_cddb`, using `cfg.drive_offset` and computed `cddb_id`.
- [x] **`src/cdda2img/config.py`** — `_prompt_create_config()` added: on first run with no
  config file and a TTY, offers to create it from `conf/cdda2img.toml.example`; re-reads the
  file on creation so the rip picks up `drive_offset` immediately.
- [x] **`conf/cdda2img.toml.example`** — fixed `"+30"` (string) → `30` (integer); added
  header comment and per-field documentation including `cddb_server`.
- [x] 85 tests passing; ruff + ty clean.

---

## ✅ DONE — Remaster provenance + create pipeline AcoustID (2026-05-07)

- [x] **`_acoustid_wavs_loop`** (new function) — per-track fingerprint loop for the `c`
  (create) pipeline. Uses the pre-transcoded `source_wavs: list[Path]` directly (track N →
  `source_wavs[N-1]`), no extraction or temp dir needed. Identical UI to `_acoustid_pcm_loop`.
  `_acoustid_menu` dispatches to it when `source_wavs` is provided; `source_pcm` path unchanged.
  `source_wavs` threaded through `run_metadata_menu` → `_fetch_menu` → `_acoustid_menu`;
  `cdda2img.py` passes `source_wavs=source_wavs` at the `create_image()` call site.
- [x] **Full release fetch in `_acoustid_run_one`** — same `lookup_release()` enrichment as
  the MB text search path: if the selected AcoustID result has `mb_release_id` and fewer
  tracks than the disc, fetch the full release before merging. Condition:
  `len(selected.tracks) < len(disc.tracks)`.
- [x] **Remaster provenance in `RBIDisc`** — four new optional fields added:
  `release_date`, `original_release_date`, `remastered_source` (default `"UNKNOWN"`),
  `mb_release_id`. All existing `RBIDisc(album=..., artist=...)` call sites unchanged.
- [x] **`_merge_into_disc` copies remaster fields** — `release_date` and
  `original_release_date` use `disc or meta` fill-in; `remastered_source` uses `meta`
  unless `disc` is already non-UNKNOWN; `mb_release_id` uses `disc or meta`.
- [x] **`_add_release_provenance(provenance, disc)`** — helper in `cdda2img.py` that
  appends `REMASTERED_SOURCE`, `RELEASE_DATE`, `ORIGINAL_RELEASE_DATE`, and `MB_RELEASE_ID`
  to the provenance dict when populated. Called in both the `c` and `i` pipelines before
  `generate_toc()`. Fields are conditionally omitted when unknown/absent so existing
  containers with no metadata lookup stay clean.
- [x] **`l` output shows Remaster line** — `_print_provenance()` in `container.py` now
  emits `Remaster: Yes (confirmed)  (this release: 2009, original: 1989)` when
  `PROVENANCE_REMASTERED_SOURCE` is present. Date parenthetical omitted when absent.
- [x] 85 tests passing; ruff + ty clean.

---

## ✅ DONE — AcoustID + MusicBrainz metadata menu fixes (2026-05-06)

End-to-end verified: import Technotronic enhanced disc, fingerprint track 4 via AcoustID,
select "Pump Up the Jam: The Album" from search, all 12 track titles + ISRCs applied.

- [x] **Per-track AcoustID fingerprint loop** — replaced auto-fingerprint-track-1 with
  `_acoustid_pcm_loop`: shows track list, extracts on demand into a temp dir (scoped to the
  full loop session), caches WAVs between calls. `_acoustid_file_loop` handles external paths.
  `_acoustid_menu` dispatches between the two based on whether `source_pcm` is present.
- [x] **Full-track WAV extraction** — `_pcm_extract_track_wav` reads the entire track into
  the temp WAV (no length cap). AcoustID uses the WAV header duration as a scoring signal;
  the earlier 120-second cap caused all candidates to be suppressed for a 322-second track.
  `fpcalc` still caps its own *analysis* window at 120 seconds internally.
- [x] **Track title visibility** — single-track AcoustID results have `TrackMeta.number=None`,
  which excluded them from `_merge_into_disc`'s number-keyed dict. `_acoustid_run_one` now
  accepts `track_number` and assigns it to single-track results before merging.
- [x] **MB invalid include fixed** — `get_recording_by_id(includes=[..., "release-groups", ...])`
  raised `"Bad includes: release-groups is not a valid include"` for a recording query, causing
  all MB chain calls to fall through to the no-album fallback. Removed `"release-groups"` from
  that includes list (valid only on release queries, not recording queries). All 4 recording
  lookups now succeed and return 1–12 releases each.
- [x] **Full release fetch on selection** — `mb_lookup.lookup_release(release_id)` added:
  calls `get_release_by_id(..., includes=["artists", "recordings", "release-groups", "labels",
  "isrcs"])` and returns a fully populated `DiscMeta` with per-track titles and ISRCs. Called
  in `metadata_menu.py` before `_merge_into_disc` whenever the selected result has
  `mb_release_id` set but `tracks=[]` (i.e. text search or release group browser results,
  which are stubs without `medium-list`). Both the MusicBrainz search and the "Find Original
  Release" paths now do the follow-up fetch.
- [x] **Verbose MB diagnostics** — `_chain_to_mb` now prints per-recording results under
  verbose mode: either `FAILED (exception text)` or `'title' — N release(s)`, making it
  straightforward to diagnose future lookup failures without enabling logging.
- [x] 85 tests passing; ruff + ty clean.

---

## ✅ DONE — DDP 2.0 import: `ddp_reader.py` (2026-05-05)

The only open-source DDP 2.0 reader for Linux. Cross-validated against cdrdao import of
the same disc (Technotronic *Pump Up the Jam*, 12 tracks): identical RG results
(−0.96 dB gain / 1.001 true-peak / 6.9 LU LRA). GEAR Pro DAT byte order verified
empirically — s16le, not s16be as DDP spec implies; `_byteswap_s16` removed entirely.

- [x] **`ddp_reader.py`** (new module) — `_parse_ddpid` (MCN from DDPID, DDP 2.x magic
  check); `_parse_pqdescr` (64-byte VVVS records: track/index/MMSSFF timing/ISRC);
  `_parse_cdtext` (block-0 CD-TEXT packs, PTI 0x80/0x81/0x86 → title/performer/disc_id);
  `_assemble_pcm` (pre-flight validates all DAT files before writing; skips 150-frame
  lead-in from TRACK01.DAT; direct s16le copy, no byte-swap); `_build_disc`; `import_ddp`
  public API returning `(RBIDisc, FLAG_MASTER_MODE)`.
- [x] **Byte order** — GEAR Pro (Windows x86) writes s16le to TRACK\*.DAT, confirmed by
  byte-level comparison of TRACK01.DAT against the cdrdao s16be BIN of the same pressing.
  No conversion needed; the DAT files are already in RBI PCM block byte order.
- [x] **`cdda2img.py`** — `i` subcommand positional argument renamed `toc_file` → `source`;
  `import_image()` branches on `source.is_dir()` (DDP) vs `.toc` extension (cdrdao); DDP
  path writes to `temp.pcm_file` directly, skipping the WAV intermediate; RG analysis and
  `build_container()` are shared between both format paths.

---

## ✅ DONE — cdrdao import pipeline: `i` subcommand, RBI v3.0, pregap support (2026-05-03)

End-to-end verified with Technotronic *Pump Up the Jam* (12 tracks, ISRC, pre-gaps,
RG block, CUE sheet, mpv CUE playback). All 25 tests pass; ruff + ty clean.

- [x] **RBI format v3.0** (breaking) — `RBITocEntry` gains `pregap_frames: int = 0`
  and `isrc: str | None = None`; `slot_timestamp` property (pregap + audio, used in
  FILE entry); `total_frames` updated to include pregap frames; `VERSION_MAJOR = 3`.
- [x] **`toc_parser.py` rewrite** — parses `CATALOG` (MCN/EAN-13; all-zeros → None),
  `ISRC`, `START` (pregap duration); `audio_start_frame` property on `ParsedTrack`;
  bare `0` BIN offset accepted alongside `MM:SS:FF` (fixes silent track-1 drop).
- [x] **`toc.py`** — `generate_toc()` writes `ISRC` and `START` lines; FILE entry
  uses `slot_timestamp` (pregap + audio) so TOC round-trips cleanly through the parser.
- [x] **`track_extract.py`** — `extract_tracks()` uses `audio_start_frame` for PCM
  slicing, correctly skipping the pregap on extract.
- [x] **`cdrdao_reader.py`** (new module) — `_byteswap_s16` (array.byteswap, O(n) C-speed);
  `convert_cdrdao_bin` (raw PCM out); `convert_cdrdao_bin_to_wav` (WAV-wrapped s16le,
  suitable for `av.open` / `replaygain.analyse`); `parsed_to_rbi_disc`; `import_cdrdao`.
- [x] **`cdda2img.py`** — `i` subcommand (`toc_file`, `--loudness`, `--output`);
  `import_image()` produces WAV intermediate for RG analysis, strips header for
  container; `_per_track_wavs()` slices raw PCM into per-track WAVs before `analyse()`
  (fixes RG block undersized for multi-track discs); `--trim-silence` /
  `--preserve-pregaps` flags for `c` subcommand.
- [x] **Bug: `.toc` file validation** — passing a `.bin` to `cdda2img i` now raises a
  clear `ValueError` instead of a `UnicodeDecodeError` traceback.
- [x] **Bug: bare `0` offset** — TOC track 1 with `FILE "..." 0 MM:SS:FF` was silently
  dropped; regex widened to `(0|\d{2}:\d{2}:\d{2})`; regression test added.
- [x] **25 tests** in `tests/test_cdrdao_reader.py` covering byte-swap, TOC parsing
  (catalog, all-zeros MCN, ISRC, pregap, bare-zero offset), BIN conversion, and full
  `import_cdrdao` integration.

---

## ✅ DONE — cdrdao CD-TEXT bug diagnosis, 1.2.6 build, byte-order clarification (2026-05-04)

### CD-TEXT garbling root cause (cdrdao 1.2.5 bug)

Burned disc had all CD-TEXT concatenated onto track 11 with disc level empty.
Diagnosis confirmed via `cd-info -T` and `cdrdao read-toc` (two independent readers).
SIZE_INFO forensics proved the cause: 15 TITLE packs observed (should be 16 with null
terminators); 13 PERFORMER packs (should be 15). Colon in PCM filename ruled out as a
cause by manual rename + re-burn with no improvement.

Exact bugs located in `dao/CdTextEncoder.cc` (1.2.5), fixed by PR #73 in HEAD:

1. **Missing null terminators** — `setRawText(const std::string&)` sized `data_` as
   `str.size()` with no null appended; `from_utf8()` similarly never called `push_back(0)`.
   Fix: `data_.resize(str.size() + 1)` + `*writer++ = '\0'`; `output.push_back(0)`.
2. **Wrong track numbers on boundary packs** — when a new string fitted in the remaining
   space of the previous pack, the encoder reused it without updating `pack.trackNumber`.
   Fix: added `lastTrack` field to `CdTextPackEntry`; tightened reuse condition to require
   `lastPack_->lastTrack == trackNr - 1` (adjacent track) or data overflows the pack.

### cdrdao 1.2.6 built from source

Cloned to `private/cdrdao`. `git checkout master` (commit d35b78d "Various CD-Text fixes").
`./autogen.sh && ./configure --without-gcdmaster && make -j$(nproc) && doas make install`.
Installed at `/usr/local/bin/cdrdao`. Re-burned disc; re-ripped confirms all 12 tracks
have correct individual CD-TEXT. SIZE_INFO now shows 16 TITLE / 15 PERFORMER packs.

### Byte order clarification (SWAP revert)

- `README.PlexDAE`: Plextor driver outputs **big-endian** (MSB-LSB). Correct burn-back
  workflow: `cdrdao write --swap` (command-line flag, not TOC keyword). This means
  cdrdao write expects **little-endian** input by default; `--swap` signals big-endian input.
- s16le (our format) is little-endian → no SWAP needed in generated TOC.
- SWAP TOC keyword was added to `generate_toc()` in a prior session in error. It also
  causes a syntax error in cdrdao. Reverted (the line was never committed).
- `cdrdao_reader.py` unconditionally byteswaps BIN → s16le: correct for standard rips
  (without `--swap`). Rips made with `read-cd --swap` must **not** be imported — the
  double-swap would corrupt audio.

### Reference material added

- `docs/reference.toc` — full cdrdao TOC grammar: all PTIs (0x80–0x8F), LANGUAGE_MAP
  codes, SILENCE vs ZERO, FILE/DATAFILE/FIFO, INDEX, FOUR_CHANNEL_AUDIO, CD_ROM/XA
  appendices, SIZE_INFO binary layout, CRC spec.
- `private/cdrdao/` — full cdrdao git clone; 1.2.5 vs HEAD diff is the authoritative
  record of the CD-TEXT encoder bug and its fix.

### Gaps identified (not yet implemented)

- `toc_parser.py` silently drops SONGWRITER, COMPOSER, ARRANGER, MESSAGE, DISC_ID, GENRE
- SILENCE / ZERO pre-gap keywords not handled in `toc_parser.py`
- Multi-language LANGUAGE blocks not preserved anywhere in the pipeline
- ISRC format not validated against ISO 3901 / the 12-character grammar in `reference.toc`

---

## ✅ DONE — Plextor PX-716A arrived and tested (2026-05-10)

Hardware is connected and working. Drive profile documented in `private/DRIVES.md`.
Resume from the Physical Media section below for remaining checklist items (C2, lead-out).

Note: the original LH-20A1S (SATA) was not usable via the FIDECO USB adapter (HDD
firmware only; no ATAPI passthrough for SATA optical drives). Replaced by a Plextor
PX-716A (IDE), which connects directly via the FIDECO's 40-pin IDE port + Molex power.

## ✅ FIXED — Null/empty track title for `38 “Heroes”.ogg` (2026-05-10)

Curly-quote characters (`“`/`”`) in the filename were converted to ASCII `”`
by `sanitize_title`, which then became the TOC string delimiter — causing `_TITLE_RE`
to capture an empty string. Fixed in two parts:

1. `toc.py:sanitize_title` now replaces any remaining `”` with `'` after all other
   substitutions, keeping the TOC grammar valid.
2. `toc.py:generate_toc` writes a `// TRACK_TITLE_UNICODE: <json.dumps>` comment
   per track when the raw title differs from the sanitized one. `toc_parser.py` reads
   this sidecar on extraction so the original Unicode title is used as the FLAC TITLE tag.

## ✅ FIXED — `read_source_rg_tags` crash on OGG Vorbis files (2026-04-28)

`metadata.py:read_source_rg_tags()` crashed with `AttributeError: ‘tuple’ object has no
attribute ‘upper’` when the source files were OGG Vorbis (`.ogg`).

**Root cause**: mutagen’s `VCommentDict` (used for OGG and FLAC tags) inherits from both
`VComment` (a `list`) and `DictMixin`. Python’s MRO resolves `__iter__` to `list.__iter__`,
so `for key in audio.tags` yields `(tag, value)` tuples — the raw list elements — not string
keys. This is documented in mutagen’s own docstring but easy to miss.

**Fix**: `for raw_key in audio.tags:` → `for raw_key in audio.tags.keys()`. `keys()` is
explicitly defined on `VCommentDict` to return unique lowercase string keys, bypassing
the list iteration. Verified working on OGG (David Bowie Platinum Collection, 3 discs)
and MP3 (Eurythmics Touch) source files.

## ✅ FIXED — SIM118 ruff warning on `audio.tags.keys()` (2026-05-10)

Ruff SIM118 suggested removing `.keys()`, but doing so breaks OGG Vorbis files:
mutagen's `VCommentDict` inherits `__iter__` from `list`, yielding `(key, value)` tuples
instead of string keys. `.keys()` is explicitly defined to return string keys and is the
correct call here. Suppressed with `# noqa: SIM118` plus an inline explanation of the
VCommentDict MRO issue. Registered in `LINT.md`.

---

## ✅ DONE — Research: Redump drive requirements + lead-in/lead-out documentation (2026-04-26)

- [x] **`private/ABHOOD.md` §5.4 added** — "CD Drive Technical Requirements for Accurate
  Dumping": scrambled-mode dumping, full P–W subchannel requirements, C2 error pointer
  semantics (MMC, not Red Book), lead-in depth (≥75 sectors, up to 150 for large positive
  write offsets), lead-out depth (≥75 sectors, more for large negative offsets), write
  offset vs drive offset distinction, `DATA_C2_SUB` vs `DATA_SUB_C2` ordering, redumper
  as preferred tool, DIC restrictions for Audio CDs.
- [x] **`private/NONSPEC.md` created** — "Lead-in and Lead-out: What They Contain, What
  They're Forced to Contain, and Where the Spec Breaks." Full technical discussion covering:
  spec-conformant lead-in layout (Q-channel TOC, P=0x00, zero main channel, CD TEXT in
  R–W); spec-conformant lead-out (P=0xFF, zero main channel, lead-out Q address); the
  pre-gap and HTOA as an intentional spec exploit; disc write offsets (manufacturing
  imprecision, Red Book does not define them, ±500–3000 samples seen in practice, how
  positive/negative offsets push audio into lead-in/lead-out respectively); drive offset
  vs disc write offset (net correction formula); copy protection attacks on the lead-in
  (Key2Audio corrupted main-channel TOC, fake second session, SafeDisc weak sectors);
  pre-mastering edge cases (non-zero lead-out main channel from early CD-R tools, why
  Redump checksums programme area only).

---

## ✅ DONE — Stale file cleanup (2026-04-26)

- [x] **Deleted** `test_normalize.py` — dead ffmpeg-normalize exploration script
- [x] **Deleted** `tests/test_transcode.py` — thin roundtrip test, superseded; better test planned
- [x] **Deleted** `src/cdda2img/unique_name.py` — dead module, not imported anywhere
- [x] **Deleted** `modules.md` — vestigial MkDocs placeholder
- [x] **Moved** `src/cdda2img/test_tui.py` → `docs/test_tui.py` — Textual TUI prototype,
  misplaced in `src/`; moved via `git mv` to preserve history

---

## ✅ DONE — Lint override register and LINT-007 fix (2026-04-27)

- [x] **LINT.md created** — documents all 10 lint suppressions and intentional unused
  variables with UIDs (LINT-001 through LINT-010), rationale, alternatives considered,
  and final decision. Every `# type: ignore`, `# noqa`, and `_`-prefixed unused variable
  in active source now carries its UID ref for cross-referencing.
- [x] **LINT-007 resolved** — `assert state is not None  # noqa: S101` in
  `replaygain.py:_measure_concat()` replaced with an explicit boundary guard
  (`if not paths: raise ValueError(...)`) at function entry. Loop refactored so `state`
  is initialised unconditionally from `paths[0]` before iterating `paths[1:]`; `ty` can
  now prove `state` is non-None at `_state_results()` without any suppression. The
  `# noqa: S101` and the `assert` are gone entirely.

---

## ✅ DONE — Update spec and format definition to v1.2 (2026-04-19)

- [x] `rbi_spec.md` — revised header layout table, removed `toc_end == pcm_start` invariant, updated validation rules, bumped to v1.2
- [x] `rbi_format.py` — new constants, updated offsets, uint64 struct format, revised dataclasses, HEADER_STRUCT with compile-time size assertion

---

## RBI Format — finalise spec before further code (agreed 2026-04-18)

### ✅ Breaking changes
- [x] Magic extended to 8 bytes: `RBIMAGE\x00`
- [x] Format version replaced with two `uint8` fields: `version_major=1`, `version_minor=2`
- [x] All four offset fields promoted to `uint64`
- [x] PCM block is now raw s16le (no WAV wrapper); WAV reconstructed on extract

### ✅ Additive fixed-header fields
- [x] `flags` (uint32)
- [x] `track_count` (uint8)
- [x] `disc_number` (uint8), `disc_total` (uint8)
- [x] `pcm_sample_rate` (uint32), `pcm_channels` (uint8), `pcm_bit_depth` (uint8)

### ✅ Structural
- [x] `toc_end == pcm_start` invariant removed; offsets are fully independent
- [x] `rbi_spec.md` updated to 121-byte fixed header layout
- [x] `rbi_format.py` updated with `HEADER_STRUCT`, compile-time size assertion, revised dataclasses

---

## Pipeline — wire up the full create/extract flow

- [x] Create `src/cdda2img/toc.py` — `sanitize_title`, `get_track_durations`, `build_toc_entries`, `generate_toc`
- [x] Create `src/cdda2img/container.py` — `build_container`, `read_header`, `extract_data`, `TempFiles`, `resolve_temp_dir`
- [x] Create `src/cdda2img/metadata.py` — `derive_album_info` with mutagen tag extraction and interactive confirm
- [x] Implement track concatenation (`concat.py`, using `wave` module — simpler and correct since format is guaranteed uniform)
- [x] Wire `normalize_pcm()` using `ffmpeg-normalize` Python API (controlled by `USE_NORMALIZATION` flag in `cdda2img.py`)
- [x] Wire up `cdda2img.py:main()` — full `c`/`x` pipeline, multi-disc support, roundtrip smoke-tested

---

## Roadmap

This document captures the agreed scope and direction for `cdda2img`. The Python
implementation is a prototype; a Rust reimplementation is planned once the design
has stabilised. Decisions should favour clarity and correctness over premature
optimisation.

### Input formats
- Physical CD-DA and mixed-mode disc (audio tracks only; data tracks discarded)
- Directory of audio files (non-recursive, any format supported by PyAV)
- RBI image file
- Foreign CDDA image files (CUE/BIN, CCD/IMG/SUB, MDS/MDF, NRG — audio component only)
- M3U, CUE, or TOC playlist/cuesheet paired with the audio files they reference

### Output formats
- RBI image file — the only officially supported write format
- Extracted TOC + raw PCM s16le (`--raw`)
- Extracted FLAC tracks with embedded metadata + CUE (`--tracks`)
- Foreign CDDA image formats — for internal testing and validation only, not distributed

### Audio processing modes
- **Master** — no processing; preserves source audio as-is
- **Remaster** — selective processing (silence trimming, EBU R128 normalisation, etc.)

---

## ✅ DONE — l/t commands, FLAG_MASTER_MODE, source RG provenance, tag case, container tests (2026-04-26)

- [x] **`l` (list) subcommand** — `list_container()` in `container.py`; prints section table
  (Fixed header / Metadata / TOC / ReplayGain block / PCM audio with offsets, human-readable
  sizes, total duration) followed by a numbered track listing.
- [x] **`t` (test) subcommand** — `verify_container()` in `container.py`; runs 27 checks:
  magic bytes, version, reserved flags, track/disc bounds, PCM params, section layout
  continuity, file size, UTF-8 metadata, SHA-256 checksums for all three blocks, TOC
  parse, track-count match. Exits with code 1 on any failure.
- [x] **`FLAG_MASTER_MODE`** (bit 2, `0x00000004`, even = "safe to ignore") — added to
  `rbi_format.py`; `FLAGS_RESERVED_MASK` updated to `0xFFFFFFFA`; `RBIHeader.is_master`
  property added; `build_container()` accepts `extra_flags`; `create_image()` passes
  `FLAG_MASTER_MODE` when `--mode master`; `rbi_spec.md` flags table updated.
- [x] **Source file RG tag provenance in TOC** — `read_source_rg_tags()` added to
  `metadata.py` (normalises ID3/Vorbis/iTunes tag name variants); `generate_toc()` writes
  `// SOURCE_RG: KEY="VALUE"` comment lines per track when tags present (cdrdao-compatible,
  ignored by TOC parser, preserved as provenance for future reference).
- [x] **Vorbis comment tag case audit** — `extract_tracks()` metadata dict corrected:
  `"title"/"artist"/"album"` → `"TITLE"/"ARTIST"/"ALBUM"` (written verbatim by libavformat);
  `"album_artist"/"track"/"disc"/"comment"` left as-is (libavformat maps these to
  `ALBUMARTIST`/`TRACKNUMBER`/`DISCNUMBER`/`DESCRIPTION` internally).
- [x] **Container roundtrip tests** — new `tests/test_container.py` (8 tests): header fields
  with/without RG, SHA-256 checksum integrity for all three blocks, TOC round-trip,
  RG block serialisation round-trip, FLAC extraction with embedded RG tags,
  `FLAG_MASTER_MODE` round-trip. All 8 pass in ~5s (module-scoped fixtures avoid repeated
  transcode + RG analysis).

---

## ✅ DONE — Subprocess elimination and audition.py cleanup (2026-04-26)

- [x] **`replaygain.py` measurement rewrite** — replaced `ffmpeg ebur128` subprocess
  (calls 1/2) with `pyebur128` + PyAV. `_decode_interleaved()` uses `AudioResampler`
  (not `to_ndarray(format=...)` which is unsupported in the installed PyAV version).
  `_measure_concat()` feeds all tracks to a single `R128State` sequentially — no concat
  subprocess, correct programme-level integrated loudness. Validated numerically against
  ffmpeg: ΔLUFS < 0.05, Δpeak < 0.005.
- [x] **`replaygain.py:embed_rg_tags()` rewrite** (call 3) — replaced `ffmpeg -c copy
  -metadata` subprocess with PyAV stream copy: reads existing FLAC container metadata,
  merges RG tags (uppercase), remuxes audio packets unchanged via `add_stream(template=)`.
  Tested: all 7 RG Vorbis comment keys present and uppercase in extracted FLACs.
- [x] **`audition.py:compute_rg()` elimination** (call 5) — deleted function; replaced
  with `replaygain.analyse([path])`. Deleted local `embed_rg_tags()` (mutagen, lowercased
  keys); replaced with `replaygain.embed_rg_tags()`. Removed `re` and `mutagen` imports.
- [x] **`audition.py:extract_clip()` rewrite** (call 4) — replaced `ffmpeg` subprocess
  with PyAV seek + decode + `AudioResampler(format="s16", layout="stereo")` + FLAC encode.
  Extracted `_window_frames()` generator (seek, skip-before-start, stop-at-end, resampler
  flush) to keep `extract_clip()` under the C901 complexity limit. Verified: 10.00 s
  output, 44100 Hz stereo, no metadata tags.
- [x] **`audition.py` lint fixes** — C901 on `main()` fixed by extracting `_handle_key()`;
  three RUF001 Unicode minus signs fixed; `_ffmpeg()` helper deleted (no longer used).
- [x] **`rbi_format.py`** — RUF003 Unicode minus in comment fixed.
- [x] **Suppress `ffmpeg_normalize` log noise** — `logging.getLogger("ffmpeg_normalize")
  .setLevel(logging.ERROR)` in `_normalize_flac()`; silences the
  "Using loudness target X because --auto-lower-loudness-target" WARNING that leaked to
  stdout mid-line. Behaviour unchanged: `auto_lower_loudness_target=True` is correct —
  it prevents clipping when true-peak headroom is insufficient for a full -18 LUFS boost.

---

## ✅ DONE — RBI v2.0, ReplayGain, and CLI refactor (2026-04-25)

- [x] **RBI spec v2.0** — bumped major version (breaking change); added `rg_start` (uint64),
  `rg_end` (uint64), `rg_checksum` (32 bytes) to the fixed header; moved `metadata` to
  offset 169; defined `FLAG_RG_PRESENT` (bit 0, even = "safe to ignore"); defined RG block
  layout (§7): 17 + 12×N bytes, column-major `track_gain`/`track_peak`/`track_range` arrays;
  added §9 validation rules 14–15; updated `rbi_spec.md` and `rbi_format.py` accordingly.
- [x] **`replaygain.py`** — EBU R128 analysis via `ffmpeg ebur128` filter; per-track
  measured independently, album measured over virtual concat; `pack_rg_block()` /
  `unpack_rg_block()` serialise to/from the binary RG block format.
- [x] **Remove `--normalize` from `c` subcommand** — dead `_normalize()` function deleted;
  `FFmpegNormalize` import removed; normalization deferred to `x --normalize` (extract pipeline).
- [x] **`--mode {master|remaster}`** — `remaster` (default): silence trim + 2-second inter-track
  gap; `master`: silence trim disabled, transcode only. `--loudness` still controls RG in both
  modes; "RG always in master" can be tightened later if needed.
- [x] **`--loudness {rg|none}`** — `rg` (default): measure EBU R128 and embed RG block in
  container gap; `none`: skip. Per-track measurement uses `source_wavs` (post-trim or
  post-transcode list) — never the concatenated blob.
- [x] **Fix `SILENCE_PAD_DUR`** — corrected `"1"` → `"2"` (Red Book inter-track gap convention).
- [x] **`build_container()` updated** — accepts optional `rg_block: bytes | None`; computes
  `rg_start`, `rg_end`, checksum, and sets `FLAG_RG_PRESENT` when block is provided; writes
  block in the gap between TOC and PCM.

---

## ReplayGain and Loudness

### Rules (never violate these)

- **Normalize and ReplayGain are mutually exclusive.** Never apply both to the same
  audio. Applying both produces incorrect output: a player will re-apply a gain offset
  to audio that has already been level-adjusted.
- **Per-track ReplayGain must be computed from individual track audio before
  concatenation**, not from the concatenated PCM blob. The concatenated blob yields
  only a single album-level measurement; per-track values require per-track audio.
- **Normalization is a delivery choice, not an archive choice.** The RBI always stores
  clean PCM. Normalization belongs at extract time only (`x --normalize`), for delivery
  to devices without ReplayGain support. Never apply normalization at create time.
- **For FLAC track extraction (`--tracks`)**: either normalize the output
  (`x --normalize`) OR embed ReplayGain tags — never both.

### Create pipeline (`c` subcommand)

- [x] **Source file RG tags** — if source files already have `REPLAYGAIN_*` tags,
  record their values as provenance metadata in the TOC (as cdrdao comments); the
  authoritative RG values in the RBI RG block are always freshly computed from the
  ingested audio, not copied from source tags.

### Extract pipeline (`x` subcommand)

- [x] **`--tracks` output** — RG Vorbis comment tags (`REPLAYGAIN_TRACK_GAIN`,
  `REPLAYGAIN_TRACK_PEAK`, `REPLAYGAIN_ALBUM_GAIN`, `REPLAYGAIN_ALBUM_PEAK`,
  `REPLAYGAIN_REFERENCE_LOUDNESS`, `REPLAYGAIN_TRACK_RANGE`, `REPLAYGAIN_ALBUM_RANGE`)
  embedded in extracted FLACs when RG block is present; suppressed when `--normalize` is given.
- [x] **`--normalize` flag on `x` subcommand** — applies EBU R128 normalisation to
  each extracted FLAC at −18 LUFS via `FFmpegNormalize`; mutually exclusive with RG tag
  embedding; no-op if `--tracks` not active.
- [x] **`--raw` output** — writes `.rg.json` sidecar alongside `.toc` and `.s16le`
  when the RBI contains an RG block.
- [x] **`--tracks` without RG block** — if RBI has no RG block (or RG checksum fails),
  compute ReplayGain from the extracted FLACs post-extraction via `analyse()` and embed
  tags with mutagen; prints album gain summary and any LRA warnings.

---

## Tests (deferred — code verified working in practice)

- [x] `input_selector.py` — tests for all four strategies (`fcfs`, `aatc`, `best`, `meta`)
- [x] `silence.py` — output shorter than input, has correct pad duration
- [x] Container roundtrip — write RBI, read back, verify checksums and track list
- [ ] Foreign format sample bank — acquire authoritative images in each supported format
  using tools in `TOOLING.md`; store in `tests/fixtures/foreign/` with confidence scores

---

## Foreign Image Format Support (deferred — needs sample files)

### Architecture principles (fixed — do not revisit)

**Read-only plugins only.** Production code ships no foreign disc image writing
capability. Writing is out of scope and carries potential IP issues. The only
output format is RBI.

**Always convert to RBI first.** Converters never operate on foreign images
directly. The pipeline is always: foreign image → RBI → extract/validate.
This guarantees a known-good, validated intermediate at every stage.

**CDDA audio scope only.** For mixed-mode discs, extract audio tracks and
discard data tracks. ISO and pure data formats are out of scope.

Reference: `private/libmirage/images/` contains parser source for all formats.
Authoritative sample images will be created using the tools listed in `TOOLING.md`
(Windows applications; for reference only).

### Converter confidence-scoring workflow

Each foreign format converter is validated and scored using the following cycle,
repeated ad hoc whenever new sample images are available:

1. Read a foreign disc image
2. Validate it against its format spec (reject malformed input early)
3. Convert to RBI
4. Extract TOC + raw PCM from the RBI
5. Re-create the foreign disc image from the extracted data *(developer harness
   only — this write path is never shipped)*
6. Validate the re-created image against the format spec
7. Update the confidence score for that converter
8. Repeat with new samples for the same format (ad hoc, when available)
9. Continue accumulating confidence over time

A high confidence score means the converter faithfully round-trips the disc
structure. Converters ship when confidence is sufficient; the score is recorded
in `tests/fixtures/foreign/README.md`.

### Formats

All formats below are read (import) targets. The developer-only write path (step 5
above) is implemented only as far as needed for round-trip validation and is never
distributed. See `TOOLING.md` for the authoritative Windows tools used to create
sample images.

**Supported in `import` today** (an in-repo reader exists):

| Format | Authoritative tool | In-repo reader | Sample | Notes |
|--------|--------------------|----------------|--------|-------|
| DDP 2.0 | GEAR Pro Mastering Edition | `ddp_reader.py` | ✅ `private/images/Gear/` | GEAR s16le byte order verified (no swap) |
| TOC/BIN (cdrdao) | cdrdao | `cdrdao_reader.py` + `toc_parser.py` | ✅ `private/images/cdrdao/` | s16be BIN → s16le swap |
| NRG | Nero Burning ROM | `nrg_reader.py` | ✅ `private/images/Nero/` | NER5 (64-bit) + NERO (32-bit); s16le (no swap) |
| CCD/IMG/SUB | CloneCD | `ccd_reader.py` | ✅ `private/images/CloneCD/` | s16be IMG → s16le swap |

**Deferred / future import targets** (sample images on hand, no reader yet — parser
reference is libmirage unless noted):

| Format | Authoritative tool | Parser reference | Sample | Status |
|--------|--------------------|-----------------|--------|--------|
| CUE/BIN | ImgBurn, EAC | libmirage | ❌ | `[ ]` |
| MDS/MDF | Alcohol 120% | libmirage | ✅ `private/images/Alcohol120/`, `Alcohol120PC/` | `[ ]` |
| MDX (+ MDS/MDF/APE) | Daemon Tools / Alcohol 120% (v6+) | libmirage | ✅ `private/images/Daemon Tools/` | `[ ]` |
| B5T/B6T/B5I/B6I | BlindWrite 5/6 | libmirage | ✅ `private/images/Blindwrite/` | `[ ]` |
| C2D | WinOnCD 6 | libmirage | ✅ `private/images/WinOnCD/` | `[ ]` |
| CDI | DiscJuggler | libmirage | ✅ `private/images/DiscJuggler/` | `[ ]` |
| CIF | Easy CD Creator / Roxio Creator | libmirage | ✅ `private/images/EasyCD/` | `[ ]` |
| BIN/CUE/XMD/XMF | CDRWIN | libmirage | ✅ `private/images/CDRWIN/` | `[ ]` |
| READCD | readcd (cdrtools/schily) | libmirage | ❌ | `[ ]` |
| M3U | — | trivial | ❌ | `[ ]` playlist paired with audio files |

*XCDRoast and Harddisk formats from TOOLING.md are out of scope: XCDRoast is a
trivial project format (implement if a sample surfaces); Harddisk is not optical.*

### Sample bank

- Store samples in `tests/fixtures/foreign/` — not committed if large
- Document acquisition steps and confidence scores in `tests/fixtures/foreign/README.md`
- Prioritise formats with the largest existing sample pools: CUE/BIN, MDS/MDF, NRG

### CLI change needed

The `import` command gains format auto-detection from file extension, plus an explicit
`--input-format` option when auto-detection is ambiguous.

---

## Physical Media / CD Drive

**Hardware connected and working**: Plextor PX-716A DVD±RW Drive (IDE), firmware 1.11
(flashed 2026-05-10), visible as `/dev/sr0`. Full drive profile and test results in
`private/DRIVES.md`. redumper binary at `private/redumper/build/redumper`.

**Drive evaluation results** (Redump 5-point checklist, 2026-05-10):
- Basic function and TOC read: ✅ PASS (12 tracks, correct durations, ISRC extracted)
- Basic CD-DA rip: ✅ PASS (confirmed via cyanrip before cdrdao testing)
- Subchannel P–W capture: ✅ PASS (PQ/raw P-W/cooked R-W all confirmed by cdrdao)
- C2 error pointer reliability: ⏳ PENDING (requires scratched disc)
- Lead-in read depth: ✅ PASS (150 sectors, meets ≥75 minimum and ≥150 preferred)
- Lead-out read depth: ⏳ PENDING (redumper PLEXTOR driver only probes lead-in)
- AccurateRip read offset: ✅ **+30 samples** confirmed (confidence ~2781, auto-applied)
- Write offset: ✅ **−30 samples** confirmed via `tools/measure_write_offset.py`
  (3 burn-read cycles, 100% confidence, 2026-05-10; see `rips/write_offset_results.toml`)
- Combined offset: **0** — self-correcting in same-drive rip+burn round-trip

### Architecture (decided)

**Reading — primary**: `cdrdao` subprocess. Produces TOC (already parsed by
`toc_parser.py`), raw PCM, and full subchannel P–W in one pass. Error correction
is adequate for clean pressed media; AccurateRip verification is the safety net.

**Reading — verification**: own AccurateRip v1/v2 checksum implementation. The
algorithms are public and short (v1: weighted 32-bit sum; v2: adds a multiply step
and different boundary conditions for tracks 1 and last). Database lookup is an HTTP
GET returning a documented binary blob. Code ports directly from Python to Rust.

**Reading — fallback**: `libcdio-paranoia` (the maintained libcdio fork of
cdparanoia) via our own thin C bindings — ctypes/cffi in Python, `bindgen` FFI in
Rust. Invoked only when a rip fails AccurateRip verification, for paranoia-grade
jitter correction and retry on damaged media. Existing Python tools (pycdio, whipper)
are not used; our own wrappers give maximum control and port cleanly to Rust.

**Writing**: `cdrdao` subprocess in the Python prototype — `.toc` + `.s16le` from
`extract --raw` map directly to `cdrdao write`. For the Rust reimplementation: `libburn`
(libburnia project), a proper C library with public headers and pkg-config support,
bound via `bindgen`. Both Python and Rust therefore share the same two underlying C
libraries: `libcdio-paranoia` (reading) and `libburn` (writing).

- [x] Test Plextor PX-716A on arrival: subchannel P–W ✅, lead-in ✅, C2 ⏳ (needs
  scratched disc), lead-out ⏳ (needs different test approach); see `private/DRIVES.md`
- [x] New `rip` subcommand: `cdda2img rip --device /dev/sr0` — rip disc to RBI via cdrdao;
  cdrdao BIN (s16be) byte-swapped to s16le; AccurateRip verified post-rip; ARIP and
  RLOG blocks written to container.
- [x] Implement AccurateRip v1/v2 checksum computation (own code, no third-party) — `accuraterip.py`
- [x] Implement AccurateRip database lookup and verify rip — informational only; no paranoia
  fallback on mismatch by design (AccurateRip CRC is a safety net, not a pass/fail gate)
- [x] New `burn` subcommand: burn RBI to physical disc via `cdrdao write`; applies write
  offset correction; reads `write_offset` from `[[drives]]` config; `--speed`, `--write-offset`,
  `--yes` options.
- [x] `drive` subcommand: unified drive management (read offset from AR catalog + write
  offset from `measure_write_offset.py` cycles; store both in `[[drives]]`)
  — DONE: superseded by `setup --read-offset` / `setup --write-offset` (commits b4ba7e6, ac9fd0f)
- [x] Extend `[[drives]]` TOML schema with `write_offset` field in `config.py`

### MCN (Media Catalogue Number)
MCN is a physical disc property (EAN-13 barcode); omit silently when the input does
not provide one. Include in the TOC `CATALOG` field when available.

- [ ] cdrdao rip input: parse `CATALOG "..."` line from `.toc` file if present
- [ ] Audio files from directory: no MCN — omit `CATALOG` line

#### Future subchannel work

Deferred 2026-06-14. Requires CloneCD `.sub` parser plumbing; revisit in subchannel work phase.

- [ ] `.sub` file input (MCN): scan for Mode 2 Q packets (ADR nibble = 0x2, TNO = 0x00), extract 13 BCD digits
- [ ] Read CD-TEXT from subchannel data (physical disc) and from `.sub` files
- [ ] Write CD-TEXT into generated TOC for CUE/BIN and RBI output
- [ ] Propagate CD-TEXT fields (performer, title, ISRC) to FLAC metadata on extract

### C2 and drive offset correction
- [ ] Verify C2 pointer support on the Plextor PX-716A (rip a scratched disc with C2
  enabled and disabled; compare — if C2 fires on known-good sectors it is unreliable
  for this unit)
- [x] Implement drive sample offset correction — `drive_offset` in
  `~/.config/cdda2img/cdda2img.toml`; applied as byte shift in `verify_rip`

---

## Metadata Strategy

The multi-source lookup chain below has shipped (CDDB, MusicBrainz, AcoustID,
Discogs, interactive confirmation menu — the R1–R16 metadata work). The
MusicBrainz track-length silence-trim guard subsection further down remains
open.

Goal: derive accurate track metadata from all available sources. Apply the following
sources in order of preference; merge where possible rather than replacing.

1. **Embedded tags** — IDv3 (MP3), Vorbis comments (FLAC/OGG), iTunes atoms (M4A),
   CD-TEXT, TOC `TITLE`/`PERFORMER` fields, CUE sheet `TITLE`/`PERFORMER`
2. **MusicBrainz lookup** — by disc ID (from TOC) or text search (album + artist)
3. **AcoustID / Chromaprint fingerprint** — fingerprint each decoded audio track,
   query the AcoustID API, resolve to MusicBrainz recording
4. **Heuristic** — infer from directory and file names (e.g. `01 - Track Title.flac`)
5. **Interactive prompt** — fall back to asking the user (existing `derive_album_info` flow)

- [x] Add `musicbrainzngs`, `pyacoustid`, and `discogs-client` to dependencies
- [x] Implement the lookup chain (`cddb.py`, `mb_lookup.py`, `acoustid_lookup.py`,
  `discogs_lookup.py`); results surfaced through the interactive metadata menu
- [x] Present conflicts to the user when sources disagree (R9 disagreement surface +
  `metadata_menu.py` confirmation menu)
- [x] Store resolved metadata in the RBI TOC and PROV blocks

### MusicBrainz track-length verification (silence trim guard)

MusicBrainz track lengths derived from the CD TOC are computed as
`INDEX_01[n+1] − INDEX_01[n]`, so they include trailing baked-in silence **and** the
pre-gap of the next track. A correct rip (pre-gap appended to previous track) will
therefore produce a local duration that closely matches the MusicBrainz length. A local
duration that significantly exceeds it indicates excess silence — typically a ripper that
appended the pre-gap on top of already-present baked-in silence.

This step runs after transcoding (accurate WAV durations available) and before
`silence.py`, and sets a per-track trim target that `silence.py` respects as a floor.

Lookup reliability, in descending order:
- **Disc ID** (from TOC input) — exact frame-accurate lengths for that pressing
- **AcoustID fingerprint** — identifies the recording; lengths may vary across pressings
- **Text search** — lowest confidence; may match a different version or regional pressing

For directory input (no disc ID), use AcoustID to resolve each track to a MusicBrainz
recording, then select the release whose per-track lengths best fit the local durations
(minimise total absolute delta across all tracks). Log the lookup method and confidence
level so the user can see the basis for any trim decisions.

Trim decision logic per track (applied in remaster mode only):

```
excess = local_duration − mb_track_length

if excess > 10s:          warn + skip (version mismatch — don't trim on MB data)
elif excess > 0.5s:       trim to (mb_track_length − STANDARD_PREGAP)
                          # MB length includes original pre-gap; pipeline re-adds it
elif excess < −5s:        warn (local is significantly shorter — different edit?)
else:                     apply standard silence-threshold trim as normal
```

- [ ] Extend `metadata.py` to return per-track lengths alongside title/artist data
- [ ] Compute per-track `excess` after transcoding; store as trim target in pipeline state
- [ ] Pass trim targets into `silence.py`; treat as a hard floor (never trim past target)
- [ ] Log lookup method (disc ID / AcoustID / text search) and per-track trim decisions
- [ ] In master mode, skip this step entirely — audio is preserved as-is

---

## Audio Processing (deferred)

### Delivery mode audition — ❌ RETIRED 2026-08-03, module deleted

`src/cdda2img/audition.py` is gone. It let a user A/B three delivery variants —
unprocessed, EBU R128 normalised, and ReplayGain-tagged — on the loudest 10-second
passage before committing, as a standalone `python -m cdda2img.audition <file>`.

**It was answering a question the project had already closed.** The loudness
processing level is not user-selectable: the standard is fixed at −18 LUFS
(ReplayGain 2.0 / ITU-R BS.1770-3), `--normalize` is extract-time only, and
`--loudness rg` measures without touching the PCM. A tool for choosing between
delivery modes should have been retired when adherence to the standard was settled;
instead it survived as 347 lines that nothing imported, shipped in every wheel and
tarball because `src/cdda2img/` is an allow-listed `DIST_PATHS` entry.

Worth recording *how* it stayed: it was never dead by the usual signal. It has a
`__main__` block, so it ran; it had passing lint entries and its own `LINT.md`
sections; `doctor` and the man page both advertised "interactive audition" as a
capability `ffplay` was required for. Every one of those made it look wired. The
only check that would have caught it is the one nobody ran — *does anything import
this?* — and the answer had been "no" since it was written.

The salvageable parts, if the question ever reopens: `find_loudest_start`
(peak-frame centring via PyAV + numpy), `extract_clip`, `normalize_clip`. See
`e6cd88e~`..`git log -- src/cdda2img/audition.py` for the deleted source.

### Master / Remaster modes
- [x] `--mode master` — silence trim disabled; transcode to Red Book spec only
- [x] `--mode remaster` (default) — silence trim enabled; `--loudness` controls RG
- [x] Fix `SILENCE_PAD_DUR = "1"` — corrected to `"2"` (Red Book 2-second inter-track gap)
- [x] Expose mode in the RBI header `flags` field (`FLAG_MASTER_MODE`, bit 2)

---

## Subprocess Elimination

Six `subprocess` calls exist across `replaygain.py` and `audition.py`, all invoking
`ffmpeg` or `ffplay`. The S603/S607 ruff warnings are suppressed as false positives
(trusted internal tool, not user input), but eliminating the subprocesses entirely
would improve portability, testability, and performance (no process spawn overhead).

Priority order: measurement (calls 1/2/5) → clip extraction (call 4) → tag
stream-copy (call 3) → playback (call 6).

---

### Call 1 & 2: EBU R128 measurement — `replaygain.py:_measure_single()` / `_measure_concat()`

Currently: `subprocess.run(["ffmpeg", "-af", "ebur128=peak=true", "-f", "null", "-"])`
and the N-file concat variant using `filter_complex`.

**Option A — `pyebur128`** (recommended)
Python bindings to `libebur128` (the reference C implementation). Correct true-peak
via 4× oversampled sinc interpolation. Workflow: decode with PyAV → feed sample arrays
to `pyebur128.Meter`. For album: feed all tracks sequentially to a single Meter instance
(no concat needed — `libebur128` accumulates state across `add_frames()` calls).
- Requires: `pyebur128` (pip) + `libebur128` (system package, e.g. `libebur128-dev`)
- Pro: reference-accurate true-peak; removes both measurement subprocesses
- Con: compiled extension + system library; slightly more setup than pure Python

**Option B — `pyloudnorm`**
Pure Python BS.1770 implementation. Workflow: decode with PyAV → numpy array →
`pyloudnorm.Meter.integrated_loudness()`. Album: concatenate numpy arrays.
- Requires: `pyloudnorm`, `numpy` (already present via PyAV)
- Pro: zero compiled dependencies
- Con: true-peak is sample peak only (no oversampling) — values in the RBI block
  will be slightly underestimated, which affects headroom calculations in players
  with hardware limiting

**Decision point**: `pyloudnorm` is fine for archival metadata (the error is small
and consistent), but `pyebur128` is the correct choice if true-peak accuracy matters.

- [x] Evaluate `pyebur128` availability and install story on target platforms
- [x] Replace `_measure_single()` and `_measure_concat()` with chosen library
- [x] Verify numerical agreement with current ffmpeg-based values on test files

---

### Call 3: FLAC tag stream-copy — `replaygain.py:embed_rg_tags()` — RESOLVED

- [x] **Resolved with mutagen** — PyAV 16 removed `add_stream(template=)` support entirely.
  `embed_rg_tags()` now uses `mutagen.flac.FLAC` to patch the Vorbis comment block in-place;
  no audio re-encoding, no temp-file dance. Simpler than either PyAV stream-copy option.
  mutagen was already a project dependency (used in `metadata.py`).

---

### Call 4: Clip extraction — `audition.py:extract_clip()`

Currently: `_ffmpeg("-ss", start, "-t", duration, "-i", src, "-c:a", "flac", "-map_metadata", "-1", ...)`

**PyAV** (direct replacement, no new dependencies)
Seek to `start` using `container.seek(int(start / time_base))`, decode frames for
`duration` seconds, re-encode to FLAC via `add_stream("flac")` — the same pattern
used in `track_extract.py:_wav_bytes_to_flac()`. The only new piece is computing PTS
from wall-clock time using the stream's `time_base`.
- Removes subprocess; consistent with existing PyAV encode pattern in the codebase
- `-map_metadata -1` equivalent: simply don't call `out_c.metadata.update(...)`

- [x] Replace `extract_clip()` with PyAV seek + decode/encode; verify clip boundaries

---

### Call 5: EBU R128 in audition — `audition.py:compute_rg()`

Duplicate of calls 1/2 (single-file measurement, same ffmpeg invocation and stderr
parsing). Once calls 1/2 are replaced, this becomes a one-line call to
`replaygain.analyse([path])` and the duplicate implementation is deleted.

- [x] After calls 1/2 are replaced: replace `compute_rg()` with `replaygain.analyse()`

---

### Call 6: Audio playback — `audition.py:Player`

Currently: `subprocess.Popen(["ffplay", "-nodisp", "-loop", "0", ...])` with
SIGSTOP/SIGCONT for pause/resume. Requires `ffplay` to be installed separately.

**Option A — `sounddevice` + `soundfile`** (recommended)
Streaming callback model: `sounddevice.OutputStream` runs a callback that reads
chunks from a decoded buffer. Pause/resume implemented via `threading.Event` —
the callback blocks on the event when paused. Volume offset applied as numpy scalar
multiply. Looping: wrap read position back to zero.
- Pure Python (with C extension); PortAudio handles platform audio
- Removes the `ffplay` dependency
- More code than SIGSTOP/SIGCONT: need a callback, a thread-safe ring buffer or
  pre-loaded array, event management, and clean teardown
- `soundfile` reads FLAC natively; no PyAV decode needed for playback

**Option B — keep `ffplay` subprocess**
SIGSTOP/SIGCONT is genuinely elegant: OS-level freeze with zero CPU, instant resume,
no buffer management. Works perfectly on Linux (the target platform). The only cost
is a second binary requirement alongside `ffmpeg`.
- Pragmatic: `ffplay` ships with ffmpeg on most distros; not a real extra dependency
- Not portable to Windows (no SIGSTOP)

Given the project's Linux focus and the TUI integration planned for `audition.py`,
keeping `ffplay` is a reasonable choice until the TUI target platform is confirmed.
Replace with `sounddevice` if Windows support becomes a requirement.

- [ ] Decide: `sounddevice` callback player vs retain `ffplay`; defer until TUI work begins
- [ ] If `sounddevice`: implement `Player` class with `threading.Event` pause/resume

---

## TUI (superseded — design notes below predate the shipped TUI)

The fixed-layout / Textual / VU-meter design sketched below has been superseded
by the TUI that actually shipped: live progress rendering is wired into the
`rip` and `import` pipelines (`--tui` / `--no-tui`), with `create` and the
metadata-menu rendering still being brought onto the same surface. The
remaining open items are tracked in the active Open section, not here. The
notes are retained for historical context.

Goal: a fixed-layout terminal UI (audio console view) wrapping the full CLI feature
set. Suggested library: **Textual** (async-native, rich widget set, good VU meter
support via `sparkline`/custom widgets).

Planned elements:
- Peak/RMS VU meter (real-time, updated during transcode/normalise)
- Track name and progress as each track is processed
- Current processing stage (transcode → trim → RG compute → pack)
- Album/artist, disc N/M, output target type
- Strategy and mode display
  (A delivery-mode audition panel was listed here; dropped 2026-08-03 with the
  prototype it referenced — the loudness standard is fixed, so there is nothing to
  choose between.)


---

## RBI Format — ongoing evaluation

Continue evaluating the spec for improvements as the implementation matures.
Borrow ideas from other formats (CUE/BIN, MDS, CloneCD) where they address gaps.

- [x] Define `flags` bit 0 (`FLAG_RG_PRESENT`) and bit 2 (`FLAG_MASTER_MODE`)
- [ ] Define remaining `flags` bit assignments: CD-TEXT present, MCN present
- [x] Embed AccurateRip checksums in the container — ARIP block; `pack_arip_block()` /
  `unpack_arip_block()`; written after every rip, readable via `l --ar` / `x --ar`.
- [ ] Evaluate whether CD-TEXT block should be a separate optional section or
  encoded within the TOC text

### Canonical TOC formatting

Currently `generate_toc()` produces correct cdrdao-compatible TOC but without
documented rules for whitespace, indentation, line endings, or field ordering.
Without canonical formatting, the TOC SHA-256 checksum is an implementation
detail rather than a content fingerprint — two logically identical containers
could have different TOC checksums if `generate_toc()` is ever changed.

- [x] Define and document canonical TOC formatting rules in `rbi_spec.md`:
  consistent indentation (2 spaces), Unix line endings, fixed field ordering
  (CATALOG before TRACK, ISRC on a fixed line within the track block, etc.)
- [x] Update `generate_toc()` to comply; add a round-trip test that verifies
  byte-identical TOC output across an RBI → parse → regenerate cycle

### Lossless round-trip invariant

Once canonical TOC formatting is in place, the following invariant should hold
and be documented in `rbi_spec.md` validation rules:

> **RBI → TOC parse → TOC regenerate → RBI** must produce a byte-identical TOC
> block (and therefore a matching SHA-256 checksum) for any container within
> the CDDA scope. Any loss across this cycle must be explicitly classified:
> *structural loss* (invalid, hard error), *metadata loss* (allowed, logged),
> or *format limitation* (documented in spec).

- [ ] Add invariant to `rbi_spec.md` §9 validation rules
- [ ] Add round-trip checksum test to `test_container.py` once canonical
  formatting is implemented

### Subchannel optional block (flag reservation only)

Raw subchannel data (P–W channels, 96 bits/sector) from physical disc rips is
valuable for CD TEXT, ISRC, MCN, and CD+G. For archival completeness it should
eventually be embeddable in the RBI container as an optional block, analogous
to the RG block.

No implementation now — this requires physical ripping hardware to be useful.
Reserve the flag bit in the spec so the assignment is stable.


### Out-of-scope disc feature support (defer to third-party tools)

Mixed-mode CD, copy-protection artefact modelling, and subchannel-aware forensic
imaging are explicitly out of scope for this tool. If ever needed, cdda2img would
delegate to established third-party tools (cdrdao for burning, DiscImageCreator
or redumper for forensic imaging) — the same pattern used for disc writing today.
No cdda2img implementation required; document the delegation point when relevant.

---

## Research Pool

Maintain a local collection of CDDA reference material in `private/`.

Current holdings:
- `private/research/IEC_60908-1999.pdf` — Red Book standard (IEC 60908:1999, second
  edition; licensed, not redistributable)
- `private/code/libmirage/` — image format parser source (MDS, CCD, NRG, TOC, CUE,
  CD-TEXT coder)
- `docs/research/spoons-audio-guide-cd-ripping.txt` — dBpoweramp Spoon's Audio Guide:
  drive features, copy protection, secure ripping practice
- `docs/research/ABHOOD.md` — A Brief History of Optical Discs; comprehensive research
  notes including §5.4: CD Drive Technical Requirements for Accurate Dumping (Redump criteria)
- `docs/research/NONSPEC.md` — Lead-in and lead-out: spec content, write offsets,
  copy-protection attacks, pre-mastering edge cases
- `docs/research/OFE.md` — The Orange Forum Embargo: Orange Book paywalling and its
  implications for open-source tools
- `docs/research/OFFSETS.md` — drive read/write offsets: sign conventions, measurement,
  combined offset, PX-716A facts (+30/−30/0)
- `private/drives/DRIVES.md` — drive list, profiles, and measured offset data

To add:
- [x] AccurateRip protocol documentation — algorithm derived from ARver `_audio.c`; disc ID
  from ARver `fingerprint.py`; URL/dBAR format from binary inspection + empirical validation
- [x] Drive read/write offsets — `docs/research/OFFSETS.md`: what they are, sign conventions,
  how to find/measure them, combined offset, cdda2img strategy, PX-716A facts (+30/−30/0)
- [ ] Reference test material: Hi-Fi grade albums (e.g. Face Value — Phil Collins) for
  ReplayGain/normalisation validation; counter-examples (e.g. Death Magnetic — Metallica)
  for worst-case loudness-war testing. Obtain lossless copies; store in
  `tests/fixtures/audio/` (not committed if large; document acquisition in a README there).

---

## ✅ DONE — Configuration

All user-tunable settings read from a TOML config file at
`${XDG_CONFIG_HOME:-$HOME/.config}/cdda2img/cdda2img.toml`.
CLI flags override config values. Config file is created on first run with
documented defaults if absent.

- [x] Create `config.py` — `Config` dataclass (`cddb_server`, `contact_email`,
  `silence_threshold`, `capacity`, `preview`, `tui`, etc.) plus per-drive `DriveConfig`
  in a `[[drives]]` array-of-tables (each carries `name`/`read_offset`/optional
  `write_offset`); `load_config()`; `_prompt_create_config()` for first-run; XDG path via
  `config_path()`. (Drive offsets live in `[[drives]]`, not a global `drive_offset` field.)
- [x] `silence = 55` — silence detection threshold in -dBFS; replaces the
  hardcoded `-55dB` literal in `silence.py:build_filter_graph`. `--silence N`
  flag on the `create` subcommand for one-off override; clamped to 1–90 with
  warn-and-default on out-of-range. TUI live-adjustable control still pending.
- [x] `capacity = 80` — disc capacity in minutes; threaded through
  `select_batches` / `batch_fcfs` / `batch_aatc` / `batch_best` /
  `_check_batch_limits` (the `meta` strategy is capacity-agnostic). `--capacity N`
  flag on the `create` subcommand for one-off override; clamped to 1–99 with
  warn-and-default on out-of-range. `MAX_RUNTIME_MINUTES = 80` retained as the
  module-level default for direct API callers and tests.
- [x] `preview = true` and `tui = true` — control track-1 audio preview and
  TerminalUI rendering on the `rip` subcommand; `--preview/--no-preview` and
  `--tui/--no-tui` flags via `BooleanOptionalAction`. TUI flag will expand to
  `create` and `import` once those pipelines are wired up.

---

## ✅ DONE — Disc Catalogue

A local SQLite database tracking all RBI images created by this user, stored at
`${XDG_DATA_HOME:-$HOME/.local/share}/cdda2img/cdda2img.db`.
Populated automatically when an RBI is created; queryable via `cdda2img catalogue`.

Schema: `catalogue` (album, artist, year, disc_number, disc_total, track_count,
mcn, remaster, mode, source, ripper, drive, rg fields, file_basename, file_path,
file_size, registered_at), `catalogue_tracks` (catalogue_id, track_number, title,
duration_frames, rg per-track fields, ar_v1_crc, ar_v2_crc, ar_status,
ar_confidence), `release_meta` (album_id, this_year, original_year, this_mcn,
original_mcn, remaster_status, mb_release_id).

- [x] Design `catalogue.py` — SQLite schema, insert/query API
- [x] Populate catalogue automatically on `c`, `r`, and `i` subcommand completion
- [x] Implement `cdda2img d` subcommand: summary, full-text search, per-disc track listing

### Release intelligence (remaster detection)

For each album created, query MusicBrainz (and optionally Discogs) to surface the
earliest known release of the same logical album and an objective low-dynamic-range
flag. The metadata menu prints a disc summary in this form:

```
  Album:    Eliminator (1983)
  Original: Yes, this release (1983)
  Artist:   ZZ Top
  MCN:      (none)
  Tracks:   11
  Low DR:   YES
```

When the disc is *not* the original release the line reads
`Original: No, <earliest title> (<year>)`; when the disc's own year is unknown or
no earlier release is found it reads `Original: Unknown, unknown release (unknown
year)`.

The original `remaster` enum (Confirmed/Possible/None, keyword + year heuristic) was
**killed** — see the 2026-05-25 DONE entry near the top of this file. It conflated
"is this a re-mastering?" with "does this sound compressed?" and answered neither
factually. It is replaced by two orthogonal, objective facts: `original_release_*`
(MusicBrainz release-group earliest release; R3-gated) and `low_dynamic_range`
(EBU R128 album LRA below `Config.low_dr_threshold`).

This lets the user know they may need to source an earlier pressing for proper
archival quality (avoiding loudness-war mastering applied to many remasters).

- [x] Implement release intelligence lookup in MusicBrainz (`original_release.py`;
  Discogs corroborates the master year via R11)
- [x] Embed result in RBI metadata (PROV `original_release_*`, `low_dynamic_range`)
  and the catalogue

---

## Source Audio Quality Check (deferred — discuss before implementing)

Detect fake-lossless source files in the `c` (create) pipeline: FLAC or WAV files that
were transcoded from lossy sources (MP3, AAC) and will degrade archival quality.

Research saved at `private/research/incoming/true-audio-checker.md`. Key findings:

- **Algorithm**: FFT spectral analysis detects the characteristic "shelf" left by lossy
  codecs above their encoding cutoff (e.g. MP3 128 kbps ≈ 16 kHz, 320 kbps ≈ 20.5 kHz).
  Tau Software's Aucdtect adds a neural network (trained via genetic algorithm) to
  distinguish lossy artifacts from intentional high-frequency rolloff in mastering.
  Accuracy: 92.4% on genuine CDDA; ~100% on obvious transcodes.
- **Key limitations**: high-bitrate MP3 (320 kbps) approaches the detection limit;
  rolled-off vintage mastering and heavily dithered audio produce false positives;
  algorithm is 44.1 kHz specific (Red Book only).
- **Integration point**: pre-transcode quality gate in `create_image()`; warn (not abort)
  by default; result stored as provenance in TOC.
- **Dependency question to resolve**: a lightweight pure-Python FFT approach needs
  `scipy` (not currently a direct dep); alternatively, optional subprocess to the
  `aucdtect` binary if installed; or a pre-trained ONNX model embedded in the package.

Proposed CLI: `cdda2img create <dir> --check-quality {warn,error,none}` (default: `warn`).

- [ ] Decide on dependency strategy (scipy / aucdtect subprocess / embedded model)
- [ ] Implement `quality_check.py` with `QualityReport` dataclass
- [ ] Wire into `create_image()` before transcode phase
- [ ] Store result in TOC provenance block; surface in `list` output

---

## ✅ DONE — Input Batching — tag-based strategy (shipped as `meta`)

The fourth batching strategy for `input_selector.py` shipped as `meta` (not `tags`).
It uses embedded disc-number metadata to recreate the original disc structure rather
than optimising for capacity.

`batch_meta()` groups tracks by their embedded disc-number tag (`DISCNUMBER` /
`TPOS`, via `_read_disc_number`), emits one batch per disc number in sorted order, and
appends any untagged tracks as a final group. The strategy is capacity-agnostic — the
planned per-disc overflow-pool handling (spill excess tracks into extra discs packed
by `best`) was **not** implemented; `meta` trusts the source disc layout verbatim.

- [x] Implement the tag-based strategy in `input_selector.py` (`batch_meta`, exposed
  as the `meta` choice on `--strategy`)
- [x] Expose `meta` in the CLI strategy selector (`--strategy {fcfs,aatc,best,meta}`)

---

## Rust Reimplementation (future)

This Python codebase is a prototype. Once the design has stabilised — formats,
pipeline, metadata strategy, and TUI layout — implement a Rust version.

Design decisions taken in Python should be made with Rust portability in mind:
- Prefer explicit data structures over dynamic dispatch
- Keep I/O boundaries clear (parsing, processing, output are separate stages)
- Avoid Python-specific conveniences that have no clean Rust equivalent
