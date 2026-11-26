# Design proposal — per-field trust model (OPT-4 + Structural C1/C2)

Status: **DRAFT — under review, no code yet.** §5 records two scope decisions, but the
self-review in **§7 (2026-06-17)** reopens them: it finds the §2.1 trust model
self-contradictory, argues C1/C2 should ship *before* OPT-4, and questions whether the
full rewrite earns its keep at all. Treat §7 as the live debate; §2–§4 are the original
proposal, annotated where §7 supersedes them.
Date: 2026-06-17.

This proposal unifies three open items that are all symptoms of one root cause:

- **OPT-4** — per-field trust-score model (replace fill-blank / first-writer-wins).
- **Structural C1** — hand-rebuilt `RBIDisc` drops physical fields.
- **Structural C2** — recording-level `mb_release_id` leaks as authoritative.

---

## 1. The problem, precisely

Today `_run_metadata_lookups` (`cdda2img.py`) folds each source's `DiscMeta` into a
single `RBIDisc` by repeated `_merge_into_disc` calls (`mb_lookup.py:705`). The merge
is **fill-blank / first-writer-wins**, so *precedence is encoded purely by call order*.
Three distinct defects fall out of that one design choice:

1. **Precedence is conflated with collection order (OPT-4).**
   - The CD-Text / on-disc baseline is the merge *target* (`disc` itself), so it wins
     every contested field **by position** — even though CD-Text is frequently wrong
     (typos, Title-Case, mojibake). The only escape is the menu's all-or-nothing
     "Overwrite All", which then clobbers fields CD-Text *did* get right.
   - There is no notion of a source being authoritative for *some* fields and weak for
     others. Discogs is authoritative for `catalog_number` / `label` / `country` but
     weak for track titles; CDDB is passable for titles but cannot split title from
     performer. The model applies a source wholesale at one rank.
   - Disagreements are silently dropped. R9 records album/artist disagreement in `prov`,
     but the user never sees "MB says X, CDDB says Y" as a *selectable* choice.
   - Every reorder is a fragile, manual precedence edit (OPT-3 was exactly this).

2. **C1 — hand-rebuilt `RBIDisc` drops physical fields.** `_merge_into_disc` /
   `_overwrite_disc` now use `dataclasses.replace` (F-001) and `_clear_disc` was fixed
   (BUG-5), so the *known* sites are safe — but the invariant is **not enforced**. Any
   new site that constructs `RBIDisc(...)` by hand silently resets `pre_emphasis`
   (R14 ≤1986 cap), `low_dynamic_range`, `disc_id`, `original_release_*`,
   `discogs_release_id` to defaults. Same defect, fixed twice, two audits apart.

3. **C2 — recording-level `mb_release_id` leaks.** Sources that identify a *recording*
   (AcoustID, ISRC tally, stage-7 duration match) must not write a pressing-level
   `mb_release_id` as if disc-ID-proven. Fixed at known sites with
   `replace(meta, mb_release_id=None)`, but again **not enforced** — it depends on every
   caller remembering to strip.

**Root cause:** an untyped, order-encoded merge with no concept of (a) per-field trust
and (b) which fields are *objective/physical* vs *guessed/metadata*. Make both explicit
per field and all three defects close by construction.

---

## 2. Proposed model — collect → resolve

Replace "fold each `DiscMeta` into `disc` in order" with a two-phase pipeline.

### 2.1 Trust levels (per *(source, field)*, not per source)

> ⚠️ **Superseded by §7.1.** The 1-D `IntEnum` below is named after *sources*, so it
> can only express a single global source ranking — it cannot say "Discogs > MB for
> `catalog_number` but MB > Discogs for `album`", which is a real requirement (MB
> populates catalogue fields at mb_lookup.py:302‑304). The corrected model is a 2-D
> `(source, field) → trust` table; see §7.1. The enum is retained here as the original
> proposal for the record.

```python
class Trust(IntEnum):
    MANUAL    = 127  # user menu entry — always wins
    OBJECTIVE = 100  # physical/computed: PCM/TOC/disc-ID/Q-channel — not a "guess"
    DISC_ID   = 80   # MB release matched by disc-ID fingerprint
    ISRC      = 70   # resolved/corroborated by per-track ISRC
    ACOUSTID  = 60   # AcoustID fingerprint
    DISCOGS   = 55   # Discogs catalogue / label / country
    DURATION  = 40   # MB text + duration fuzzy (stage-7)
    CDTEXT    = 35   # on-disc CD-Text (frequently wrong)
    CDDB      = 20   # gnudb free text (lowest)
```

Trust is assigned **per (source, field)**. A source registers proposals only for the
fields it is competent at, at a field-specific trust. Examples:

| Source   | Proposes (field @ trust)                                                   |
|----------|---------------------------------------------------------------------------|
| Ripper   | `isrc@OBJECTIVE`, `catalog@OBJECTIVE` (Q-channel), `pre_emphasis@OBJECTIVE`, `disc_id@OBJECTIVE`, `low_dynamic_range@OBJECTIVE` |
| CD-Text  | `album@CDTEXT`, `artist@CDTEXT`, `track.title@CDTEXT`                      |
| MB disc-ID | `album@DISC_ID`, `artist@DISC_ID`, `track.title@DISC_ID`, `mb_release_id@DISC_ID`, `mb_release_group_id@DISC_ID` |
| AcoustID | `mb_release_group_id@ACOUSTID` (**never** `mb_release_id`)                 |
| Discogs  | `catalog_number@DISCOGS`, `label@DISCOGS`, `country@DISCOGS`               |
| Stage-7  | `album@DURATION`, `track.title@DURATION`, `mb_release_group_id@DURATION` (**never** `mb_release_id`) |
| CDDB     | `album@CDDB`, `track.title@CDDB`                                           |

Per-(source,field) trust is the substantive advance over "precedence by call order".

### 2.2 Phase 1 — collect proposals

Each source emits `FieldProposal(field, value, trust, source)` objects instead of a
half-populated `DiscMeta` that gets folded in. (Adapter `meta_to_proposals(meta, source)`
lets existing source code keep returning `DiscMeta` during migration — see §4.)

### 2.3 Phase 2 — resolve

For each field, the highest-trust proposal wins. Near-ties (equal or within a band,
different value) are retained as **alternatives** attached for the metadata menu. A single
canonical assembler `disc_from_resolution(resolution, base_disc)` writes the result with
`dataclasses.replace`.

---

## 3. How this closes all three defects

- **OPT-4** — highest-trust-wins *per field*, independent of collection order. A wrong
  low-trust value can no longer block a high-trust one. Reordering the pipeline stops
  being a precedence lever (the whole OPT-3 class of edits disappears). Disagreements
  become menu alternatives instead of silent drops.

- **C1** — physical fields are proposed at `OBJECTIVE` by exactly one producer (the
  ripper/TOC parser) and resolved like any other field. The single `disc_from_resolution`
  assembler uses `replace`, so a new metadata source physically *cannot* drop a physical
  field — it isn't allowed to propose it, and `OBJECTIVE` outranks everything but `MANUAL`.

- **C2** — a recording-level source's allowed proposal set simply omits
  disc-level `mb_release_id` (it may propose `mb_release_group_id`). The "strip pressing
  MBID" rule becomes "the field isn't in the source's proposal schema" — enforced by the
  API, not by remembering to strip.

### 3.1 Bonus — dissolves the OPT-3 tradeoff

OPT-3's residual tradeoff (a CDDB-only-seed disc never reaches stage-7) is a *lookup
gating* problem — stage-7 needs an album/artist seed to run its MB text search — **not** a
merge-precedence problem. The trust model lets us **decouple seed-from-merge**: collect
CDDB's album as a search seed *and* run stage-7, while trust (not order) decides the merge.
That conflation is exactly what OPT-3 had to trade off; the trust model removes it.

---

## 4. Migration (incremental, low-risk)

- **Phase A — enabling, zero behaviour change.** Add `Trust`, `FieldProposal`,
  `resolve_proposals()`, and `disc_from_resolution()` (the C1 assembler) + an invariant
  test asserting physical fields survive a resolve. No pipeline rewiring.
- **Phase B — adapter swap.** Add `meta_to_proposals(meta, source)` driven by the
  per-(source,field) trust table. Replace the `_merge_into_disc` chain in
  `_run_metadata_lookups` with collect→resolve. The existing precedence tests
  (`test_parallel_pre_menu.py`) become **order-independence** tests: when the trust table
  mirrors today's order, behaviour must match — that is the regression gate.
- **Phase C — payoff.** Surface near-tie alternatives in the metadata menu; feed the
  structured resolution into `build_match_distance` (its contributors already align:
  `mb_disc_id +0.50` ↔ `DISC_ID`, `mb_duration_match +0.20` ↔ `DURATION`, etc.), replacing
  the current `prov` string-sniffing; decouple stage-7's seed from merge (§3.1).

`match_distance` is **extended, not replaced** — it consumes a structured resolution
instead of sniffing `prov` keys.

---

## 5. Decision points

> **Reopened by §7.** The two decisions below were taken before the self-review. They
> are not reversed, but §7.2 (resequencing — C1/C2 first) and §7.5 (is the full rewrite
> justified?) put their *timing and scope* back in play pending the debate. The
> enforcement decision (typed schema) is unaffected and stands.

1. **Scope** — ✅ **DECIDED 2026-06-17: full collect→resolve (Phases A–C)** — *timing
   under review, see §7.2/§7.5.*
2. **Enforcement of C1/C2** — ✅ **DECIDED 2026-06-17: typed proposal schema.** A source
   may only emit its allowed fields; reintroducing C1/C2 becomes a type/validation error,
   not a silent bug. `FieldProposal` is `frozen`; `field` is an enum; OBJECTIVE-only
   fields are rejected from metadata sources at construction.
3. **Trust source** — *default taken:* fixed enum table in code for the first cut (tune
   only after Phase B reproduces current behaviour); `Config` override deferred. Revisit
   if the user wants it configurable sooner.
4. **Menu alternatives** — *default taken:* deferred to **Phase C** (the resolver retains
   near-ties from Phase A, but the menu UI consumes them only in C).

---

## 6. Risks / honest caveats

- The trust table *is* the policy; getting a number wrong silently changes which source
  wins a field. Mitigation: Phase B must reproduce current behaviour exactly (the
  order-independence tests are the gate), so the first cut encodes today's de-facto
  precedence and is tuned only afterwards.
- Track-level fields (`title`, `performer`, `isrc`) need per-track resolution, not just
  disc-level — the proposal key is `(track_number, field)` for those. More plumbing than
  disc-level fields; account for it in the Phase A type design.
- `OBJECTIVE` ISRC/MCN come from the Q-channel only on the *rip* path; on *import* and
  *create* paths they come from the foreign image / tags and are **not** objective. Trust
  assignment must be per-pipeline, not hard-wired to the field. (This is a real subtlety
  the current model dodges by accident; the trust model must handle it explicitly.)

---

## 7. Review findings (2026-06-17) — open questions before Phase A

Self-review of §1–§6 against the code. Verdict: the collect→resolve *shape* is sound and
§3.1 (decouple search-seed from merge-precedence) is the strongest idea, but the central
trust mechanism in §2.1 is self-contradictory, two of the three bundled problems don't
need the rewrite, and the payoff is narrower than §1 implies. Do not start Phase A until
§7.1, §7.2 and §7.5 are settled.

### 7.1 The trust model must be 2-D `(source, field) → trust`, not a 1-D source enum

§2.1 calls trust "per (source, field)" but defines a 1-D `IntEnum` whose levels *are*
sources. "Per-field" is then faked by which fields a source proposes. That breaks on a
verified real case: **MB populates `country` / `label` / `catalog_number`**
(mb_lookup.py:302‑304) and would sit at `DISC_ID=80`, while **Discogs — the catalogue
authority — sits at `DISCOGS=55`.** Global source rank makes MB win catalogue fields over
Discogs, which is wrong; the only 1-D escape is to artificially forbid MB from proposing
catalogue data it genuinely has (R16 barcode hints). The honest model is a 2-D table with
per-source defaults and explicit per-field overrides:

| field                 | Objective (rip) | MB disc-ID | Discogs | AcoustID | stage-7 | CD-Text | CDDB |
|-----------------------|:---------------:|:----------:|:-------:|:--------:|:-------:|:-------:|:----:|
| `album` / `artist`    | –               | 80         | 50      | –        | 40      | 35      | 20   |
| `track.title`         | –               | 80         | –       | –        | 40      | 35      | 20   |
| `catalog_number`/`label`/`country` | –  | 60         | **80**  | –        | –       | –       | –    |
| `catalog` (MCN)       | **100**         | 55         | 50      | –        | –       | (CDTEXT)| –    |
| `track.isrc`          | **100**         | 70         | –       | –        | –       | –       | –    |
| `mb_release_id`       | –               | 80         | –       | –        | –       | –       | –    |
| `mb_release_group_id` | –               | 80         | –       | 60       | 40      | –       | –    |
| `pre_emphasis`, `disc_id`, `low_dynamic_range`, `original_release_*` | **100** | – | – | – | – | – | – |

Note the **inversion** the 1-D enum can't express: MB > Discogs for `album`, Discogs > MB
for `catalog_number`. A `dict[(source, field), Trust]` with a `dict[source, Trust]`
fallback is barely more code than the enum and is *correct*. **Open question:** adopt the
2-D table, or accept the catalogue inversion and stay 1-D for simplicity?

### 7.2 C1/C2 are enforcement bugs and should ship *before* OPT-4

C1 ("use `replace`, don't hand-build `RBIDisc`") and C2 ("strip pressing `mb_release_id`")
are *proven, recurring* bugs (each fixed at ≥2 sites). They need a **typed objective-vs-
metadata field classification + an invariant test** — a small, self-contained change that
does **not** require the resolver. §4 sequences them behind the *speculative* OPT-4
precedence rework. That is backwards. **Proposed resequencing:** land C1/C2 enforcement as
a standalone first step (real bug-class closure, low risk), then do OPT-4 deliberately.

### 7.3 OPT-4's payoff is concentrated in `--auto`; §1's framing overstates it

Per the project ethos (the user is the final arbiter in the menu; PCM verbatim is the real
guarantee), in the **interactive** path the user fixes any field regardless of merge
precedence — so OPT-4's precedence correctness changes the *final result* only in
`--auto` mode, plus the new menu-alternatives feature. §1 leads with "wrong CD-Text blocks
better MB," implying pervasive interactive harm the menu already neutralises. Honest
scope: this rework buys **(a)** elimination of an order-dependence *bug class*, **(b)**
`--auto` correctness, **(c)** menu alternatives — *not* better interactive guesses.

### 7.4 "Low-risk, tests are the gate" is too optimistic — pin edge cases first

Phase B rewires the merge every rip/import/create path depends on, gated by "reproduce
current behaviour." But current behaviour includes accreted edge logic the existing suite
may not pin: the `"Unknown Artist"` sentinel treated as blank (mb_lookup.py:711); the ISRC
validate-and-fallback chokepoint (R13, lines 726/789); empty-string-vs-`None` "presence"
semantics; and the rip-vs-import `OBJECTIVE` distinction (§6). A clean resolver tends to
drop exactly these and rediscover them as bugs. **Mitigation:** write characterization
tests pinning these *before* any rewrite, so the gate has teeth. Also: `--auto` needs a
deterministic tie-break for equal-trust/different-value (no menu), which reintroduces a
stable source order — so "order-independent" has an asterisk.

### 7.5 The "if it ain't broke" test — is the full rewrite justified?

Against the project's `metadata_over_engineered` principle (guard against cost/complexity
that doesn't improve the guess), the full collect→resolve rewrite must clear a bar: does it
*improve outcomes*, or just *re-implement the same outcomes differently*? Honest tally:

- **Genuinely better:** closes the order-dependence bug class for good (no more OPT-3-style
  manual precedence edits); enforces C1/C2 by construction; enables menu alternatives;
  fixes the catalogue inversion (only with the 2-D table); dissolves the OPT-3 seed/merge
  tradeoff (§3.1).
- **Not better:** the interactive final result (user already arbitrates); everyday discs
  where one disc-ID match supplies everything (the resolver and the current merge produce
  identical output).

Decision to put to the optimisation-advisor and the user: **(i)** do nothing (current
merge works for the common path; fix only OPT-3-class issues ad hoc); **(ii)** minimal —
ship C1/C2 enforcement + the §3.1 seed/merge decouple, skip the resolver; **(iii)** full
collect→resolve with the 2-D table. The §5 decision chose (iii); §7 asks whether (ii) is
the better cost/benefit point.

---

## 8. Optimisation-advisor refinement (2026-06-17)

Full report: `private/optimiser/2026-06-17T18-27-02_trust-model-design-debate.md`.
Its bottom line — **recommend (ii) minimal, plus a targeted rule §7/§9 missed** — and the
load-bearing claim were both verified against the code here.

1. **§7.1 is overstated — the catalogue inversion is latent, not live.** Verified:
   `RBIDisc` (`rbi_format.py:305-335`) has **no** `catalog_number` / `label` / `country`
   fields. MB (`mb_lookup.py:302-304`) and Discogs (`discogs_lookup.py`) both populate them
   on their transient `DiscMeta`, the menu *displays* them, and `_merge_into_disc` /
   `_overwrite_disc` then **drop them** (they aren't in either `replace()` call). The
   MB-vs-Discogs "inversion" therefore has **zero committed effect**. The 1-D-enum mechanism
   critique is logically valid but its motivating example evaporates.

2. **Zero live cross-field inversions among *persisted* fields.** Walking `RBIDisc`'s
   actual fields: `album`/`artist`/`track.title` share one consistent ranking (no flip);
   `catalog` (MCN) is the only genuinely contested persisted field, and it already has a
   bespoke check-digit-ranked resolver (`_collect_barcode_candidates` /
   `_pick_canonical_mcn`, `cdda2img.py:987-1049`), not order-by-position. So the 1-D-vs-2-D
   question is **moot for the code as it stands** — removing (iii)'s defining advantage.

3. **The real harm is narrow and non-interactive.** The order-dependent merge is the
   *committed* result under two triggers — `--auto` and non-TTY/scripted runs
   (`menu_state.py:1023`: skip when `not isatty() or auto_apply`). (Historical note: an
   earlier design also auto-skipped the menu on a STRONG confidence match; that was retired
   on 2026-06-20 — match confidence is now display-only and never skips the menu, so STRONG is
   no longer an auto-commit trigger.) Within those, the
   only wrong outcome is a **present-but-wrong CD-Text baseline disagreeing with a
   disc-ID-matched MB release, committed with no human in the loop.** Interactively, the
   user arbitrates and precedence is irrelevant. (This widens §7.3, which named only
   `--auto`.)

4. **The missed option (the most coherent fix for that harm).** A **single pipeline-aware
   rule**: a disc-ID-matched MB release **overwrites a *present* baseline** album / artist /
   track-title **on the rip path only** (CD-Text baseline is poor); fill-blank everywhere
   else (the create path's baseline is the user's curated mutagen tags, which *should* keep
   winning). One conditional in `_run_metadata_lookups`; no framework, no table, create path
   untouched. Captured as **B2** in §9.

5. **§7.2 / §7.4 confirmed.** C1/C2 should ship first and the *standalone* fix is a
   chokepoint + invariant tests — the typed schema only pays off once the resolver exists.
   The accreted edge cases to pin first are real and the list is longer than §7.4 had:
   add the **inconsistent presence semantics** (`disc.album if disc.album` falsiness vs
   `disc_number ... is not None`, `mb_lookup.py:753-758`) and the **unconditional MCN
   override** in Discogs phase A (`cdda2img.py:1102-1103`, *not* fill-blank).

6. **Separate bug surfaced (orthogonal to the trust model).** The menu shows the user
   Discogs `label` / `catalog_number` / `country` that are then silently discarded (no
   `RBIDisc` fields hold them). Either persist them (a format/spec change) or stop showing
   them. Captured as **B6 / Decision D4** in §9. *Not yet independently confirmed at the menu
   display path — verify before acting.*

7. **The one honest payoff of (iii).** Menu *alternatives* ("MB says X, CDDB says Y — pick
   one") — a genuine UX feature, the only thing (iii) buys that (ii) cannot. It is a
   *feature, not a fix*, retrofittable later, and `metadata_over_engineered` weighs against
   paying a full-rewrite price for it now. Captured as **B5 / Decision D2**.

---

## 9. Open design decisions — for discussion (NO decision yet)

This section is the live agenda. Nothing here is decided; §5's earlier choices are
explicitly reopened (see §5 banner). The work decomposes into independent **building
blocks**; the three options (i)/(ii)/(iii) are bundles of them.

### 9.1 Building blocks

| ID | Block | Fixes | Cost | Depends on |
|----|-------|-------|------|-----------|
| **B0** | Characterization tests pinning current merge behaviour (sentinel, ISRC fallback order, presence semantics, MCN override) | Nothing directly — *de-risks* every other block | Low | — |
| **B1** | C1/C2 enforcement: a single `replace`-based metadata-merge chokepoint + `_strip_pressing_mbid` + invariant tests | C1 (dropped physical fields), C2 (leaked pressing MBID) — *proven recurring* | Low | B0 |
| **B2** | Pipeline-aware rule: disc-ID MB overwrites *present* CD-Text baseline on **rip** only | The narrow auto-mode harm (§8.3) | Low | B0 |
| **B3** | §3.1 seed/merge decouple: let CDDB seed stage-7's search while merge order is unchanged | The OPT-3 CDDB-only-seed tradeoff | Low–Med | — |
| **B4** | Full collect→resolve resolver + `(source,field)` trust (1-D or 2-D) + `disc_from_resolution` assembler | Order-dependence *as a class*; subsumes B1/B2 via construction | **High** | B0 |
| **B5** | Menu alternatives UI (surface near-ties for user pick) | — (new feature) | Med | B4 |
| **B6** | Dropped Discogs catalogue fields: persist, or stop showing | The shown-then-discarded bug (§8.6) | Low (stop-show) / Med (persist + spec) | — |

### 9.2 Option bundles

- **(i) Do-nothing** — none (optionally B0 opportunistically). Accept the auto-mode harm
  and the latent C1/C2 reintroduction risk. MCN resolver + OPT-3 reorder already in place.
- **(ii) Minimal** — **B0 + B1 + B2** (+ optionally **B3**). Fixes every *proven/real* harm
  with no framework. Defers menu alternatives.
- **(iii) Full** — **B0 + B4 + B5** (B1/B2 subsumed by construction). Larger change; its
  unique payoff is B5 (menu alternatives); the 2-D table is moot (§8.2) unless B6-persist is
  also chosen (see D5).

### 9.3 Decisions to take

- **D1 — Direction.** (i) / (ii) / (iii). *Leaning (ii) per §8; recommendation not binding.*
- **D2 — Menu alternatives (B5).** Defer (retrofit later) vs. build now. This is the swing
  factor for (iii): if B5 is wanted, (iii) becomes justifiable; if not, (ii) dominates.
  *Open sub-question:* is B5 genuinely retrofittable onto the current merge later, or does
  deferring make it materially harder? (Working assumption: retrofittable via a small
  near-tie side structure — to be confirmed.)
- **D3 — §3.1 seed/merge decouple (B3).** Worth it, or leave OPT-3's tradeoff as the
  accepted rare case? *Open sub-question:* how often does a CDDB-only-seed disc actually
  occur (no CD-Text, no MB/Discogs/AcoustID, CDDB hit)? If ~never, skip B3.
- **D4 — Dropped catalogue fields (B6).** Track-for-later / persist-now / stop-showing.
  *Prerequisite:* confirm the fields are actually shown-then-discarded at the menu display
  path (not yet independently verified). *Spec note:* persist = spec-before-code (bump
  `rbi_spec.md`, add fields to `RBIDisc`, merge + emit).
- **D5 — Coupling between D4 and D1.** If D4 = persist, the MB-vs-Discogs catalogue
  inversion becomes **live** again, which (a) revives part of §7.1's argument and (b) needs
  *either* a one-line "Discogs wins these three" rule (cheap, fits (ii)) *or* the 2-D table
  (only justified inside (iii)). So D4-persist slightly strengthens — but does not by itself
  justify — (iii). Decide D4 before finalising D1.

### 9.4 Points still needing deeper consideration (flagged by the user, 2026-06-17)

- Whether B2's "overwrite present baseline" is always right for a disc-ID match, or whether
  there are pressing-variant cases where CD-Text formatting is preferable (low risk —
  disc-ID is pressing-specific — but worth a worked example).
- The exact `--auto` / non-TTY tie-break policy if any future block introduces
  equal-trust contests (the "order-independent" asterisk, §7.4). (STRONG is no longer a
  trigger — match confidence became display-only on 2026-06-20.)
- Whether `metadata_over_engineered` should veto B5 outright, or whether menu alternatives
  are the rare UX feature that *does* improve the guess (the user is the arbiter, and
  alternatives improve what the arbiter sees).

- **(User, 2026-06-17) Reframe D4 as a disambiguation question, not just persist-vs-drop.**
  The real question about the shown-then-dropped fields (`label` / `catalog_number` /
  `country`, and any other candidate data) is: **does surfacing more data *assist
  disambiguation*, or only introduce more irresolvable conflicts?** More fields = more
  cross-source agreement signals (could sharpen a weak match) *but also* more contested
  fields with no committed home (more noise, more `metadata_over_engineered` risk). Next
  session: weigh disambiguation value vs. conflict cost *before* deciding persist/show/drop.
  This likely wants a worked example — a real disc where catalogue data would have changed
  the chosen release.

- **(User, 2026-06-17) Preserve the "genuinely elegant solution"; refine, don't replace.**
  The agent called the typed-schema enforcement "genuinely elegant" (report §7.2, re. §5.2:
  OBJECTIVE-only fields rejected at `FieldProposal` construction). User preference:
  **non-destructive** evolution — refine or add to the existing design only where it adds
  *real* value; do not break or rip out what already works (the bespoke MCN resolver, the
  current merge's correct-for-create behaviour, the typed-schema idea). Bias toward
  additive B1/B2/B6 over the wholesale B4 rewrite unless a concrete benefit demands it.
  *(Confirm next session which "elegant solution" the user meant if ambiguous — typed-schema
  enforcement vs. the existing bespoke MCN resolver.)*

### 9.5 Worked example for D4 + a release-selection rung (2026-06-19)

The D4 worked example §9.4 asked for now exists: `docs/reference/DISAMBIGUATION.md` — an
18-step manual interrogation of a real disc (U2 *The Joshua Tree*) against all five services.
**No decision taken here; this records findings to refine against.**

**D4 verdict from the worked example.** Catalogue data (`barcode`/`country`/`catalog_number`)
**cannot identify** a pressing — byte-identical same-master pressings are fundamentally
indistinguishable from the disc, *even with* on-disc metadata (the disc resolved to 5
byte-identical 1987 releases differing only in packaging/catalogue). It **can** drive a
deterministic, user-controllable, reproducible *preference* among indistinguishable candidates.
So **persist (D4=persist) is justified for labelling + provenance, not disambiguation power** —
it sharpens the *label*, not the match. This answers §9.4's "assist disambiguation vs add
conflict?": neither — it enables a *preference*, which is a third thing.

**User proposal (2026-06-19) — a release-selection rung (Layer 1).** A gap this surfaces: §2
resolves per-*field* but has **no release-selection step** for multiple disc-ID matches with
differing catalogue values (MB returns 5 countries, not one). Proposed last-resort rung, below
the existing `_disambiguate_by_isrcs` → `_resolve_via_isrc_tally` → `duration_match_lookup`
cascade (all return None on this disc):
1. **plurality barcode** (most common normalised barcode scores highest), then
2. **`preferred_country`** config (ordered MB country codes; priority ranking, *not* a filter;
   unlisted = lowest equal priority), then
3. terminal tiebreak (proposed: earliest date, then MB release-ID).
Output is a **scored candidate set** (no discards) feeding the consensus model.

**How it fits the landscape:**
- **Pure scoring, not hard-narrow** (advisor-reviewed): a hard barcode cut would drop the only
  uniquely-barcoded release before `preferred_country` could rescue it. Popularity must weight,
  never gate.
- Fits **(ii)-minimal + B6-persist**; it is **additive, not a rewrite** — aligns with the
  "refine, don't replace" preference above. ⚠ **Corrected in §10.3:** the rung is **not** a
  refinement of `_pick_canonical_mcn`. That helper only has *barcodes* in scope, so it
  structurally cannot host keys (2) `preferred_country` / (3) date / (4) MB-ID — those need full
  release records. The rung is a **new terminal rung in the `mb_lookup` disambiguation cascade**
  (operating on the full `DiscMeta` candidate records); `_pick_canonical_mcn`'s `candidates[0]`
  default only fires on the **no-MB-release path** (on-disc / Discogs-only). The overlap is just
  key (1) barcode-plurality, which the rung subsumes upstream.
- Answers the §9.4 **equal-trust tiebreak** open point: all disc-ID matches are equal `DISC_ID`
  trust, so trust can't separate them; this cascade is the tiebreak.
- `preferred_country` is **not a trust level** (those rank source reliability) — it's an
  orthogonal user-preference prior applied only at release-selection. Config-dependent output ⇒
  record the applied preference in PROV (R10 reproducibility).

**Refinements (2026-06-19, confirmed with user):**

- **Pure lexicographic scoring** (no hard cuts). Release-selection key chain, each breaking
  ties left by the one above: **(0) on-disc MCN match [objective] → (1) barcode-plurality
  [popularity prior] → (2) preferred_country [user pref] → (3) earliest date → (4) MB
  release-ID [terminal]**. MCN-above-plurality = evidence outranks proxy (mirrors `OBJECTIVE >
  everything`). Endorsed consequence: a uniquely-barcoded region pressing ranks *below* the
  common-barcode tier even for a matching `preferred_country` (preferred_country arbitrates
  only *within* a barcode tier).

- **B6 scope is larger than "catalogue fields" and partly a display gap.** Verified against
  `RBIDisc` (rbi_format.py:304-335):
  - `release_date` (this release) **already exists and is populated** — but `list`/`catalogue`
    only surface `original_release_*`. **Display gap, no spec change** — just render it.
  - `label`, `country` are **genuinely missing** → add (spec-before-code).
  - label catalogue number: field `disc_id` exists but is **CD-Text PTI 0x86 only** and its
    name collides with "MB Disc ID" / `mb_release_id`. Add `catalog_number` (from MB/Discogs)
    and **rename `disc_id`** (e.g. `cdtext_catalog_ref`) to kill the ambiguity.
  - `catalog` (MCN/EAN-13 barcode) vs `catalog_number` (label's own number, e.g. `CID U2 6`)
    are distinct → separate fields.

- **Provider-role model (precedence = granularity, not flat trust).** Disambiguation is a
  cascade through granularities; each provider sits at one. **(1) TOC/local** defines the
  candidate set (disc) → **(2) AccurateRip** excludes wrong masters (master/era; high but
  coarse) → **(3) ISRC/duration** (from MB) narrow recording/timing *when discriminating* →
  **(4) MB catalogue scoring** (barcode-plurality → preferred_country) picks a *preference* →
  **(5) age/MBID** terminal. Roles: **MB** = release enumerator + catalogue substrate (primary;
  everything scores *its* candidates); **Discogs** = per-candidate corroborator via the
  MB→Discogs url-rel (raises field confidence / surfaces conflict; feeds cross-source barcode
  plurality) — *not* a selector; **AcoustID** = recording/track labeller + gross-mismatch
  sanity (≈0 edition power, remaster-robust); **CDDB** = free-text fallback (≈0 edition power).
  **Key finding:** for byte-identical pressings the non-MB providers add ~nothing to
  *edition* disambiguation — the edition choice rests on MB catalogue data + preference config.
  That is the knowability ceiling, not a fixable gap. (Worked example: DISAMBIGUATION.md §4.)

**Locked decisions (2026-06-19) — feed these into B6/§10 when written:**

- **Barcode > country** (preponderance-of-evidence; the guess is most defensible from the
  weight of evidence). Lexicographic, confirmed.
- **AcoustID = gate, not selector.** Add an audio-corroboration gate over the MB candidate set
  ("does the audio match the claimed album at all?"); ~0 edition power, but catches wrong
  disc-ID / TOC-collision / mispress. Distinct from the selection cascade.
- **`preferred_country` = TOML array** `["GB","XE","US"]`; empty/unset ⇒ skip the key.
- **Rename `disc_id` → `cdtext_catalog_ref`** (collides with "MB Disc ID"/`mb_release_id`) and
  add `catalog_number`. spec-before-code (`rbi_spec.md`). Plus add `label`,
  `country`; surface the already-stored `release_date` in `list`/`catalogue`.
- **Migration = clean break (user, 2026-06-19).** Bump the RBI format version, but **no read
  shim / no backwards compatibility** — old `disc_id`-bearing containers are *not* migrated or
  read. A breaking field rename with no compat path conventionally implies a **major** bump
  (v5.0 → v6.0); confirm major-vs-minor when writing §10. Rationale: prototype, Rust reimpl
  pending, no production `.rbi` corpus to preserve. Removes the entire dual-name read path from
  scope — `read_header`/TOC/PROV parse only the new field name.
- **(a) Discogs role: CLOSED 2026-06-19 → barcode-only corroboration.** Built
  `tools/compare_mb_discogs.py` (MB↔Discogs field-mismatch tally via the url-rel join) and ran a
  broad corpus (95 seed albums → 74 comparable; raw at
  `private/research/incoming/mb_discogs_corpus_2026-06-19.txt`). Normalised mismatch rates:

  | field | mismatch | reading |
  |-------|:--------:|---------|
  | **barcode** | **0/63** | perfect agreement — the gold standard, confirmed at scale |
  | label | 15/71 (21%) | **genuine structural** disagreement (imprint vs parent `Capitol`/`Beastie Boys Records`; reissue labels `Atlantic`/`Rhino`, `Columbia House`, `DeAgostini`; sublabels `Skam`/`Warp`) — *not* vocabulary |
  | catalog_number | 13/71 (18%) | noisy (multiple sleeve codes; the two services pick different ones — see catalogue-code taxonomy) |
  | country | 34/70 (49%) | half vocabulary the normaliser misses (`CA`/`Canada`, `JP`/`Japan`, `BR`/`Brazil`), half genuine multi-region scoping (`GB`/`Europe`, `US`/`Europe`) |
  | year | 3/54 (6%) | mostly agree; sparse |

  **The broad run overturns the earlier 5-release sample on `label`** (then 0/5, now 21%
  structural). ⇒ **Discogs gets selection weight on `barcode` only** — feeds cross-source
  barcode-plurality (disambiguator key (1)). `label`/`country`/`catalog_number` are still
  *persisted + displayed* (B6) for labelling/provenance, but carry **no** disambiguation weight
  and trigger no cross-source corroboration. Decision (a) is settled; no further corpus run
  needed.

**Metadata-presentation invariant (2026-06-19).** Expanding stored fields must be paired with a
**single canonical renderer** shared by the metadata menu (creation landing), `catalogue`, and
`list`: (1) stored ⟺ displayed (no gaps both ways); (2) the same field set in all three;
(3) identical format/spacing/order so the three are visually interchangeable. This is a
display-layer requirement riding alongside B6-persist (relevant to D4, and to §8.6's
shown-then-discarded bug — the fix is "persist *and* render consistently", not "stop showing").

---

## 10. Implementation specification (B6-persist + release-selection rung + canonical renderer)

Status: **SPEC — ready to implement once `rbi_spec.md` is bumped (spec-before-code).** Every
gating decision in §9.5 is locked; this section turns them into a buildable unit. Advisor-reviewed
2026-06-19 (integration architecture + scope discipline). Grounded against the current code at the
call sites named below.

### 10.0 Scope

In: **(a)** expanded catalogue fields + the `disc_id` rename + RBI version bump (clean break);
**(b)** the lexicographic release-selection rung + `preferred_country` config; **(c)** the AcoustID
gate; **(d)** the single canonical metadata renderer across menu / `catalogue` / `list`.

Out (unchanged from §9): the full B4 collect→resolve resolver; B5 menu *alternatives* (no consumer
under (ii) — the rung picks a single top candidate and does **not** build scored-set machinery);
the 2-D trust table (moot — only `catalog` (MCN) is a contested *persisted* field and it keeps its
bespoke check-digit resolver). B1/B2 (C1/C2 enforcement) are independent and may land separately;
§10 does not depend on them.

### 10.1 Format change — RBI v6.0 (spec-before-code)

**Version bump: v5.0 → v6.0** (major; breaking field rename, no read shim — §9.5). The reader
**rejects** a container whose major version ≠ 6 rather than translating old fields. Update
`rbi_format.py:VERSION_MAJOR = 6`, `VERSION_MINOR = 0`, and `docs/reference/rbi_spec.md`
**before** any code.

**`RBIDisc` field changes (`rbi_format.py:305-335`):**
- **Rename** `disc_id` → `cdtext_catalog_ref` (PTI 0x86 CD-Text catalogue/label ref). *Python
  attribute only* — the cdrdao TOC keyword stays `DISC_ID` (cdrdao grammar; `toc.py:128`,
  `cdrdao_reader.py:93`, `toc_parser`). The rename kills the collision with "MB Disc ID" /
  `mb_release_id`.
- **Add** `catalog_number: str | None = None` — the label's own catalogue number (e.g. `CID U2 6`),
  distinct from `catalog` (the MCN/EAN-13 barcode). From MB/Discogs.
- **Add** `label: str | None = None`, `country: str | None = None` — from MB (Discogs corroborates
  barcode only — §9.5(a)).
- `release_date` (this release) **already exists and is persisted** (`_add_release_provenance`,
  `cdda2img.py:534`) — no field change; it is a *display* gap only (§10.5).

**Three persistence surfaces must move together** (this is the C1-style invariant restated for the
new fields — every stored field needs a write site, a read site, and a catalogue column):
1. **PROV block** — `_add_release_provenance` (`cdda2img.py:519`) gains
   `catalog_number`/`label`/`country` keys (same `if disc.X:` pattern); the read side in
   `container.py` (~963) reconstructs them onto `RBIDisc`. `cdtext_catalog_ref` is unaffected (it
   rides the TOC, not PROV).
2. **Catalogue SQLite schema** — `catalogue.py` schema + `register_rbi` + the `_show_record`
   SELECT (`catalogue_menu.py`) gain `label`/`country`/`catalog_number` columns. **The catalogue
   is a derived index, NOT an RBI container — it self-migrates additively** (the RBI clean-break
   does *not* apply here). Bump `_SCHEMA_VERSION` and add a `_migrate_v4_to_v5` (`ALTER TABLE ADD
   COLUMN`) chained in `_check_schema_version`, exactly like the existing `_migrate_v3_to_v4`.
   *(Lesson learned, 2026-06-20: the first cut added the columns to the DDL but forgot the
   version bump + migration; `CREATE TABLE IF NOT EXISTS` is a no-op on an existing DB, so the
   columns silently never appeared — crashing the catalogue browser and silently breaking rip-time
   registration. An existing catalogue.db must never be invalidated by a schema change.)*
3. **`rbi_spec.md`** — document the new PROV keys, the rename, and the v6.0 bump.

### 10.2 Config — `preferred_country`

`Config.preferred_country: list[str] = field(default_factory=list)` (`config.py`). TOML array,
e.g. `preferred_country = ["GB", "XE", "US"]`. Semantics: an **ordered priority ranking, not a
filter** — listed codes rank in order; unlisted codes share the lowest equal rank; empty/unset ⇒
key (2) is skipped entirely. Add a commented entry to `conf/cdda2img.toml.example`. Because output
becomes config-dependent, the applied preference is **recorded in PROV** (R10 reproducibility — see
§10.3).

### 10.3 Release-selection rung (Layer 1) — lexicographic cascade rung

**Where it lives (verified):** a new terminal rung inside `_prepop_multimatch` (`mb_lookup.py:1195`),
reached when `_resolve_multimatch` returns `winner is None` — i.e. the existing ISRC/MCN cascade
(`_disambiguate_by_isrcs` → `_resolve_via_isrc_tally`) could not pin a pressing. The cascade site
already holds the **full `DiscMeta` candidate records** (`matches: list[DiscMeta]`, carrying
`catalog`/`country`/`release_date`/`mb_release_id`), so the rung is one more call with the same
argument — **no plumbing**.

**Candidate set — score the album-consistent subset, NOT all `matches` (advisor-corrected,
load-bearing).** The branch being refined does more than "decline to pin": before the agreed-facts
merge it narrows to `subset = mcn_hits-or-all` then to the **plurality release-group**
(`mb_lookup.py:1240-1247`). That narrowing is **TOC-collision protection** — a disc-ID can match
different *albums* (the `_albums_match` "Eliminator" / "Afterburner / Eliminator" case), and on an
MCN/ISRC-less disc the `_is_consistent` gate passes a minority wrong-album candidate vacuously.
Scoring all `matches` would let key (3)/(4) pin that wrong-album collision (distinct barcodes ⇒ no
plurality ⇒ falls to earliest-date/lowest-MBID, which the collision can win). The AcoustID gate is
too coarse to backstop an *album-level* error (it only suppresses `--auto`; interactive still
pre-fills wrong). **The rung therefore scores within `subset ∩ plurality-RG`** — the same
album-consistent set the agreed-facts logic already establishes — and pins the best *pressing of the
album the code already identified*. JT target case unaffected: all 5 finalists share the Joshua Tree
RG → all 5 scored.

**RG-tie tiebreak — barcode plurality (added 2026-07-09, user-approved).** When the candidates split
*evenly* across release-groups (`_plurality_release_group` returns None — e.g. ABBA *Gold: Greatest
Hits* vs *Forever Gold*, two different compilations sharing one disc-ID), the album is still
recoverable from the candidates' own barcodes: a barcode held by a strict plurality (≥ 2 releases,
unique max) that resolves to a **single** RG pins that RG (`_plurality_release_group_by_barcode`,
wired as `_plurality_release_group(matches) or _plurality_release_group_by_barcode(matches)`). This
is the `barcode_plurality` key doing *RG-establishment*, not just pressing selection — a principled
extension of the same same-namespace signal (§1a). It fires only on positive barcode evidence: a
plurality barcode spanning two RGs, or a barcode tie, still declines, so the TOC-collision protection
above is preserved (no barcode plurality ⇒ no pin ⇒ leave blank for the menu). Discovered via the
ABBA Gold "release date 1974" bug: MB applied nothing on the 2/2 RG tie, so CDDB's `DYEAR` (1974)
filled the blank. NB this was *downstream* of a separate, prerequisite bug — `disc_id_from_rbi`
computed the lead-out from `total_frames` (sum of pregap+duration), omitting a track-1 head offset
(this disc's `start_frame=33`) → wrong MB disc-ID → 404 → the multi-match path never ran at all.

**Behaviour change (call out + test):** the rung **refines the agreed-facts path** — instead of
"merge only the facts every candidate agrees on" over that set, it "picks the best pressing within
that set" (pins `mb_release_id` + that release's catalogue fields). Previously `mb_release_id`
stayed unset on this branch (the agreed-facts merge); the rung now pins it (implemented in
`_prepop_multimatch`). Defensible under the best-guess authority model
([[project_metadata_authority_model]]): preference-driven, PROV-recorded, user-correctable in the
menu. But it changes *committed* output on the `--auto` / non-TTY path → **characterization test
required** (§10.7).

**The scoring (pure lexicographic, no discards — pick top):** key chain, each breaking ties left by
the one above. (A former key 0 "on-disc MCN match" was **removed** 2026-06-30 — the on-disc MCN is
archival only and never a disambiguator, §1a; selection rests on the candidates' own service
barcodes, a same-namespace comparison.)
1. **barcode-plurality** [popularity prior] — the most common normalised barcode across the
   album-consistent subset scores highest. **MB-internal plurality is the default** (on the JT disc this alone
   discriminates: `042284229821` ×3). Discogs corroboration (§10.3.1) is a *light* signal on the
   chosen release, **not** N per-candidate url-rel fetches.
2. **`preferred_country`** [user pref] — rank by position in the config array; arbitrates only
   *within* a barcode tier (a uniquely-barcoded regional pressing stays below the common-barcode
   tier even if its country is preferred — endorsed consequence, §9.5).
3. **earliest `release_date`**.
4. **MB release-ID** [terminal] — guarantees a unique deterministic winner. "Arbitrary but
   reproducible" is acceptable here (user-confirmed).

On selecting `winner`, merge via `_merge_into_disc(winner, disc)` (same as the ISRC-winner path) and
return it as `meta`. **C2 is satisfied, not violated:** setting `mb_release_id` here is
pressing-level-legitimate — every candidate shares the disc-ID fingerprint (unlike the
AcoustID/duration paths that must null it).

**PROV:** record `release_selected_via` (the key that broke the tie: `barcode_plurality`/
`preferred_country`/`date`/`mbid` — the on-disc MCN is never a selection key, §1a) and, when
`preferred_country` fired, `preferred_country_applied`.

### 10.3.1 Discogs corroboration (barcode-only, light)

Per §9.5(a) (closed: barcode 0/63; label/country/catalog_number unreliable). On the **selected**
release only, follow the MB→Discogs url-rel and compare `barcode`; on agreement raise a confidence
note, on conflict surface it (PROV `discogs_barcode_conflict`). This feeds cross-source barcode
plurality only if a worked example shows MB-internal plurality *tying* where cross-source breaks it
— otherwise MB-internal plurality stands alone (≈0 extra network; `metadata_over_engineered`).

### 10.4 AcoustID gate (corroboration, not selection)

Role: a **post-selection, set-level** sanity check — "does the disc audio match the album the
disc-ID matched at all?" — to catch wrong disc-ID / TOC-collision / mispress. AcoustID is
remaster-robust and has ≈0 *edition* power, so it cannot rank pressings; it gates the *trust in the
disc-ID match as a whole*. Reuse the existing `_r6_acoustid_corroborate` machinery (tracks 1 and
ceil(N/2)).

**Ordering (verified):** the rung lives inside `prepopulate_from_mb`
(`_run_metadata_lookups:1496/1503`); `_r6_acoustid_corroborate` already runs **after** it, at
`_run_metadata_lookups:1553`. So the gate corroborates the **already-chosen** release and gates the
auto-commit decision — it runs *after* selection, **not** before scoring. Do **not** plumb
fingerprinting into `prepopulate_from_mb` (that would be a far larger change); the gate is a check
applied **once** at the existing R6 site, against the selected release — not a per-candidate filter.

Semantics (mirrors the AccurateRip precedent — informational, never fails the rip):
- **Pass** → proceed with selection. **Fail** (audio does not corroborate the matched album) →
  emit a WARNING + PROV `acoustid_gate=failed`, and **suppress `--auto`** (do not auto-commit a
  release the audio contradicts; on a TTY this drops through to the interactive menu for review).
  (Implemented policy, finalised 2026-06-20: **warn-only** — the disc-ID result is always kept and
  flagged in PROV, never reverted. On a headless `--auto` / non-TTY run with no menu the flagged
  result is still committed, not left unapplied; revisit if a real failing disc shows a better
  policy. See `_gate_adjusted_auto`, `cdda2img.py:1391`.)
- Threshold: corroboration counts as pass when ≥1 probed track's AcoustID recordings include the
  matched release-group; otherwise fail. (Tune after a worked example; default conservative =
  warn-only unless `--auto`.)

### 10.5 Canonical metadata renderer (the three presentation invariants)

The three sites render **different field sets from different sources** today:
- **menu** (`metadata_menu.py:90-143`) — from transient `DiscMeta`; already shows `release_date`
  ("Released:") and `catalog_number` — the most complete.
- **`list`** (dispatch `cdda2img.py:2633`; cf. the `_print_source_info` ad-hoc block at `763`) —
  from `RBIDisc` (via PROV on read); the least.
- **`catalogue`** (`catalogue_menu.py:_show_record`, `291`) — from **SQLite columns**; shows
  `original_release_*` + year + MCN.

**Scope of the three invariants:** they govern the **disc-level header block** only. The per-track
table (#, Title, Duration, ISRC) is explicitly out of scope — only menu + `list` render it, the
`catalogue` summary does not, and that asymmetry is intentional (so invariant (2) is not
self-contradictory).

"One canonical renderer" is therefore **a shared formatter fed a normalised dict**, not one
function reading one object (the three data sources differ). Define:
`format_disc_metadata(fields: DiscDisplayFields) -> list[str]` — a single pure function returning
the formatted lines; each site builds the `DiscDisplayFields` from its own source (RBIDisc / DiscMeta
/ SQLite row) and prints the shared output. Invariants enforced by construction:
1. **stored ⟺ displayed** — the field set is exactly the persisted set from §10.1 (no shown-then-
   discarded fields → closes §8.6; no stored-but-hidden fields → surfaces `release_date`).
2. **same set in all three** — all three call `format_disc_metadata`; divergence becomes impossible
   without editing the one function.
3. **identical format/spacing/order** — defined once in `format_disc_metadata`.

The canonical field set (order): Album (+ this-release year from `release_date`) · Artist · Label ·
Country · Catalogue no. (`catalog_number`) · MCN (`catalog`) · Original release (`original_release_*`)
· Tracks (count + duration). Per-track table (#, Title, Duration, ISRC) stays site-local (only menu
+ list show it; not the catalogue summary).

### 10.6 Test plan

- **Characterization (write first, per §7.4/§8.5):** pin current `_prepop_multimatch` agreed-facts
  behaviour on a multi-match-no-winner fixture **before** adding the rung, so the behaviour change is
  intentional and visible in the diff.
- **Rung unit tests:** key (0)–(4) each decisive in isolation; the JT-shaped fixture (3× shared
  barcode + 2 unique) → barcode-plurality tier → `preferred_country` GB → expected MBID; empty
  `preferred_country` skips key (2); terminal MB-ID guarantees determinism.
- **Round-trip:** v6.0 container with `catalog_number`/`label`/`country` set → `build_container` →
  `read_header` → fields reconstructed; `cdtext_catalog_ref` survives the TOC round-trip.
- **Renderer:** `format_disc_metadata` golden-output test; assert menu / `list` / `catalogue` all
  call it (no residual ad-hoc field printing).
- **Gate:** pass and fail fixtures; assert `--auto` suppressed on fail.
- Run on min Python (3.10) per [[feedback_verify_min_python]].

### 10.7 Build sequence

1. `rbi_spec.md` bump + `RBIDisc` field changes + PROV write/read + catalogue schema (§10.1) — the
   format foundation, spec-first.
2. `format_disc_metadata` + wire all three sites (§10.5) — makes the new fields visible everywhere.
3. `preferred_country` config (§10.2).
4. The rung + characterization test + PROV provenance (§10.3).
5. Discogs barcode corroboration (§10.3.1) + AcoustID gate (§10.4).

Steps 1–2 are pure B6 (fields + display) and independently shippable; 3–5 are the disambiguator.

---

## 11. B4 authoritative build plan (DECIDED 2026-06-21)

**Decision: build B4 (full collect→resolve, resolver as sole committer) — the
authoritative path, true order-independence as a class.** User chose it over the
cheaper "shadow-collect for B5 only" fork, eyes-open that the *behavioural* payoff
beyond B5 is ~nil (the order-dependence only bites the B2-dead case: CD-Text
present *and* disagreeing with MB) — the value is the architecture + B5.

**Scope: Layer 2 (per-field value merge) only.** §10's release *selection* (the
lexicographic rung, `_pick_canonical_mcn`, AcoustID gate, catalogue fields, the
canonical renderer) already shipped and stays untouched. B4 replaces the
`_merge_into_disc` fold *beneath* it. "Refine, don't replace."

### 11.1 The entanglement (why this is not "replace a fold")

The merge is **mutate-as-you-go**, distributed across `prepopulate_from_mb` (4
`_merge_into_disc` sites), `_prepopulate_from_discogs`, `_r6_acoustid_corroborate`,
then stage-7 and CDDB in `_run_metadata_lookups` — and later stages **read
accumulated disc state mid-pipeline**. There are **three** such Layer-1-dependent
reads of `disc.mb_release_id`, all running *before* the (future) final resolve:
- stage-7 gate (`cdda2img.py`): `if disc.mb_release_id is None and (disc.album
  or disc.artist)`.
- AcoustID corroboration (`_r6_tally_and_merge`) compares against `disc.mb_release_id`.
- §10.3.1 Discogs-barcode corroboration (`_discogs_barcode_corroborate`) follows the
  selected release's MB→Discogs link via `disc.mb_release_id`.

(§11.1 originally listed only the first two; the Discogs check is the third —
enumerated here as of B-2.) A naïve "collect every `DiscMeta`, resolve once" breaks
all three.

### 11.2 The decoupling (the crux)

`mb_release_id` is *both* an eager **gating signal** (Layer-1 "which release") and
a deferred **resolved field** (Layer-2). Cut it cleanly:
- **Layer-1 selection stays eager** — `prepopulate_from_mb` decides the release and
  exposes it as `mb_result.selected_release_id`. The stage-7 gate, AcoustID
  corroboration **and the §10.3.1 Discogs-barcode check** read *that result*, not a
  mutated `disc`.
- **Layer-2 field values defer** — each source contributes `FieldProposal`s to an
  accumulator; one `resolve()` + `disc_from_resolution()` commits at the end. The
  single `Resolution` carries cross-source alternatives → B5.

### 11.3 "Reproduce today" trust mapping (the gate's foundation)

Today's behaviour is **fill-blank / baseline-wins-when-present**, which is *not* a
trust contest. Reproduce it as: the baseline (CD-Text on rip / mutagen on create)
contributes proposals **at max trust for the fields it actually has**, and
contributes **nothing** for the meta-priority quirk fields (`disc_number`,
`disc_total` — where `_merge_into_disc` is meta-first). Network sources rank in
today's call order (MB → Discogs/AcoustID → stage-7 → CDDB). The "Unknown Artist"
sentinel is cleaned to empty by the adapter so it never wins. `resolve()` then
reproduces `_merge_into_disc` field-for-field — **proven by the B0 characterization
+ `test_parallel_pre_menu` as the byte-identical gate.** B-6 flips the baseline low
(CDTEXT < DISC_ID) for the corrected ranking — the *only* behaviour change, with
its own characterization.

### 11.4 Strangler staging (each step keeps all tests green)

- **B-1** — **LANDED 2026-06-22** (`resolver_adapter.py` + `test_resolver_adapter.py`,
  commits `7299a2d`/`8b867c5` + the Hypothesis follow-up). `trust_for` (flat two-tier
  reproduce map), `meta_to_proposals` (skip-before-construct, so an empty
  recording-level `mb_release_id` never hits the C2-raising constructor),
  `baseline_proposals` (max-trust disc accumulator; sentinel + ISRC-validate quirks;
  `disc_number`/`disc_total` abstention reproduces meta-priority). `Field.ORIGINAL_RELEASE_DATE`
  added. Equivalence test spans all 17 fields, both polarities, both collection orders,
  + a Hypothesis property test over the live in-domain space. **Three documented
  divergences** (see §11.6 gate): two strict-xfail (invalid-disc-ISRC scrub;
  duplicate track numbers) and one representational class (falsy-but-present `""`/`0`)
  excluded by the property strategy. Per the 2026-06-22 decision these are **deferred
  to B-6** — which gates the B-4 flip (below).
- **B-2** — **LANDED 2026-06-22.** `selected_release_id` exposed on `MBPrepopResult`,
  captured once at the `prepopulate_from_mb` chokepoint from the merged
  `disc.mb_release_id` (so it equals what the gates read today, by construction,
  across every sub-path incl. the ISRC-tally `strip_pressing_mbid` → None case).
  **All three** mid-pipeline gates switched to read it (not just the two §11.1
  named): stage-7 gate, AcoustID corroboration (`_r6_tally_and_merge`, threaded via
  `_r6_acoustid_corroborate`; create path passes `disc.mb_release_id`), and §10.3.1
  `_discogs_barcode_corroborate`. Behaviour-neutral by construction — verified
  empirically: the gates are exercised by direct unit tests (`test_r6_corroborate`,
  `test_discogs_corroborate`, `test_original_release` R6, `test_parallel_pre_menu`
  asserts `selected_release_id == disc.mb_release_id`). Verified: `_prepopulate_from_discogs`
  never writes `mb_release_id`, so nothing mutates it between the prepop and the gates.
- **B-3** — **shadow mode**, split in two:
  - **Part 1 — model + proof — LANDED 2026-06-22** (`test_merge_sequence.py`). The
    reproduce-today trust map went flat→distinct (per-source call-order ladder;
    baseline lowered OBJECTIVE so the §10 canonical MCN can sit above it; the
    "Unknown Artist" sentinel emitted at a sub-CDDB `_SENTINEL_TRUST`). A Hypothesis
    property test proves `resolve(accumulated) == ` the full live merge *sequence*
    (baseline → MB → canonical-MCN overwrite → Discogs → stage-7 → CDDB) on the live
    in-domain space. It found a real divergence on its first run — a *meta*-sourced
    sentinel (the skip-based handling only covered the baseline) — now fixed. Greps
    settled the two predicted complications: (C) disc_number last-writer is
    **unreachable** (Discogs lacks it, MB⊕stage-7 exclusive, CDDB has none);
    (B) catalog **is** §10-owned (canonical MCN overrides) → the trust-ladder fix.
    AcoustID confirmed to merge no fields (asserted), hence absent from the sequence.
  - **Part 2 — production accumulator — LANDED 2026-06-23** (`_collect_metadata_proposals`
    + the `_shadow_out` seam in `_run_metadata_lookups`; `test_shadow_equivalence.py`).
    The MB `DiscMeta` was already exposed (`MBPrepopResult.meta`, R8); the only source
    still trapped was the Discogs `hit` + chosen MCN, so `_prepopulate_from_discogs`
    now returns `(disc, chosen, hit)` (hit is `None` on every path that did not merge
    one). `_run_metadata_lookups` deep-copies the pre-merge baseline at entry, captures
    `chosen`/`hit`/`stage7_meta`/`cddb_meta`, and — **gated behind `_shadow_out is not
    None`** (the proposal build can raise on a C2 violation; an unused shadow path must
    never abort a best-effort metadata run) — collects proposals, resolves, and stashes
    `{disc, resolution, proposals, baseline}`. Three integration tests drive the **real**
    function and assert `disc_from_resolution(resolve(acc), baseline) == live_disc`,
    newly verifying against running code (vs. part 1's reconstruction): the **full**
    `_r6_acoustid_corroborate` wrapper is field-neutral (forced down its
    `is_available()==False` early-return; the merge branch stays covered by
    `test_merge_sequence`), `_prepopulate_from_discogs`'s phase-A/B disc effect is
    reproduced from `(chosen, hit)`, and MB's disc effect equals
    `_merge_into_disc(meta, baseline)`. The fixtures are clean `DiscMeta`, so exact `==`
    holds and **neither strict-xfail divergence is reachable** — no allow-list machinery
    was needed (the modulo is the existing strict-xfails in `test_resolver_adapter`).
    At B-4 the live merges are deleted and the function returns `_shadow_out["disc"]`.
- **B-4** — **flip — LANDED 2026-06-23.** `_run_metadata_lookups` now returns the
  resolver's committed disc (`disc_from_resolution(resolve(_collect_metadata_proposals(
  ...)), sanitize_base(baseline))`), unconditionally and in production. The resolve
  build is wrapped in `try/except` → fallback to the legacy fold (C2 cannot fire on
  the live domain, but metadata is best-effort and must never abort a rip).
  **The per-source merge chain is RETAINED, not deleted** (deferred to a post-soak
  follow-up), demoted to three roles: it feeds the mid-pipeline lookups their search
  context (Discogs album-match, the stage-7 seed — these *consume* merged state, so
  the merges cannot simply vanish), it is the never-fail fallback, and its output
  `merged` is the live equivalence **oracle**. The B-4 gate was the two B-1
  divergences (dup-track reproduce-today; invalid-ISRC uniform-drop via
  `sanitize_base` — §11.6); both resolved, so the flip changes no behaviour on an
  undecided axis (full suite byte-neutral). `create_image` does not call
  `_run_metadata_lookups`, so it is unaffected (its only field merge is the
  proven-neutral AcoustID corroborate + the menu, which is B-5).
  - **Anti-tautology (advisor 2026-06-23):** after the flip the returned disc *is*
    the resolver output, so `test_shadow_equivalence` was repointed to assert
    `returned == _shadow_out["merged"]` (resolver vs the retained legacy fold) — a
    real check, not resolver-vs-itself. When the merges are eventually deleted, this
    test retires and the golden `test_parallel_pre_menu` / B0
    `test_merge_characterization` (which pins `_merge_into_disc` directly) carry the
    guard — a conscious step, not silent erosion.
  - The ranking change (baseline low) stays separate as B-6 — it is the only
    *intentional* trust reordering and gets its own characterization diff.
- **B-5** — **LANDED 2026-06-23.** The three interactive menu source-merge applies
  (`menu_state.py` `_apply_mb` / `_apply_discogs` / `_apply_acoustid`) now route
  through `resolver_adapter.apply_menu_selection` instead of `_merge_into_disc` /
  `_overwrite_disc`. Architecture (advisor): the menu is sequential/imperative while
  the resolver is declarative, so `ctl.disc` is the live baseline re-resolved on each
  apply — no persistent accumulator (that is B-7). **Update** = the B-1 keystone with
  `disc = ctl.disc` (selection at the below-baseline `Source.MENU` trust fills blanks);
  **overwrite** = the new keystone (selection at `Trust.MANUAL`, via
  `meta_to_proposals(trust_override=…)`, outranks the OBJECTIVE baseline). `Source.MENU`
  is **not** recording-level, so a user-selected release's `mb_release_id` is kept
  (user-confirmed) rather than C2-stripped — matching today's bake. Proven unwired
  first (`test_menu_apply_resolver.py`: both keystones + two 400-example Hypothesis
  properties), then the three sites flipped; behaviour-neutral except the accepted
  resolver-cleaner divergences (a *selected* "Unknown Artist" demoted below a real
  baseline value; the shared ISRC carve-outs — which do not arise from pipeline data:
  rip/import `ctl.disc` is the B-4 committed disc (ISRC-sanitised) and create-path
  `build_toc_entries` sets no per-track ISRC, so they surface only via a manual edit,
  where the divergence *is* the approved drop/normalise). **Out of scope (deferred to
  B-7):** per-field/per-track
  direct edits, `_clear_disc`, and revert stay direct `ctl.disc` mutation — modelling
  typed values as MANUAL proposals only earns its keep once the accumulator is carried
  into the menu. The C1 `_apply_original` / `_set_original_manually` stay direct
  mutation by design (those fields are not in the `Field` enum — the C1 enforcement).
- **B-6** — **LANDED 2026-06-23 — scoped down by user decision.** The single
  intentional ranking change reduced to **one per-(source, field) override**: Discogs
  outranks MB for the catalogue **pair** `catalog_number` / `label`
  (`resolver_adapter._FIELD_TRUST_OVERRIDE`; Discogs=80 > MB=60, both below the
  OBJECTIVE baseline so a present on-disc value still wins fill-blank). This fixes the
  **live D5 inversion** that v6.0 created by persisting those fields — MB had won only
  by merge call-order. Pipeline-independent; every other `(source, field)` falls
  through to the reproduce-today map.
  - **`country` is excluded** (user decision 2026-06-23). Unlike catalog_number/label
    it couples across layers: §10 Layer-1 may select the MB pressing *because* its
    country matched `preferred_country`, so a Layer-2 Discogs override would undo that
    preference. `country` stays reproduce-today (MB > Discogs), honouring the §10
    selection.
  - **The old "baseline-low" / B2 rule was DROPPED.** B2 = "disc-ID MB overwrites a
    *present* CD-Text album/artist/title on rip." Its §8.4 premise ("CD-Text baseline
    is poor") was overturned by later research ([[reference_cdtext_reliability]]):
    CD-Text's problem is *absence*, not wrongness — when present it is reliable, and
    the online DBs are the error-prone source. Overwriting present CD-Text with MB
    would be a regression, so the CD-Text/disc-baked baseline is **not** lowered. This
    resolves the open §9.4 question in favour of CD-Text.
  - **Characterization diff (authored, not discovered):** the resolver now
    *intentionally* diverges from the legacy merge for the catalogue triple when MB and
    Discogs both supply a value. `test_merge_sequence`'s reproduce-legacy property
    carves Discogs catalogue out of its domain (still proves equivalence elsewhere);
    the contested case is asserted positively in `test_b6_*` (unit, sequence, and the
    real-pipeline `test_shadow_b6_discogs_catalogue_beats_mb_diverges_from_legacy`).
    The `trust_for(source, field, pipeline)` *pipeline* seam is retained but unused.
- **B-7** — B5 menu alternatives UI (the confirmed destination), fed by
  `Resolution.alternatives`; feed the structured resolution into
  `build_match_distance` (replaces PROV string-sniffing); the §3.1 seed/merge
  decouple (B3) falls out for free.

### 11.5 Traceability (per-field decision provenance) — user requirement 2026-06-21

"If both B5 choices are wrong, which item in the scoring system caused it?" The
resolver must answer this. The fill-blank merge discards losers (only R9 logs
album/artist disagreement); the resolver retains everything by construction, so
the requirement is to *expose* it, spanning both layers:

- **Resolver retains the full contender set per field**, not just the winner +
  de-duped alternatives. `resolve()` already groups proposals by key internally;
  surface it as `Resolution.contenders[key] -> tuple[FieldProposal, ...]`
  (all non-empty, trust-desc). The B5-facing `alternatives` (distinct-valued
  losers) stays a derived view.
- **Skipped proposals are recorded with a reason** (empty value, "Unknown Artist"
  sentinel, etc.) — so a *silently dropped* correct value (failure mode 2) becomes
  visible rather than invisible. Capture as e.g. `Resolution.skipped[key] ->
  tuple[(FieldProposal, reason), ...]`.
- **A per-field provenance dump** exposes it: extend `list --prov` (and/or a
  `--explain` mode) to print, per field, `winner (source@trust)`, the contenders,
  the skipped+reason, and the Layer-1 `release_selected_via`. This localises any
  wrong result to exactly one of: source/data gap · a drop · Layer-1 selection ·
  the trust ranking.
- **Honest limit:** the trace explains *where the value came from and what
  competed*; it cannot invent a value no source proposed — but it makes that case
  unambiguous (points at the data gap, not a phantom code bug).

Implication for B-1: `FieldProposal` is already the carrier; the resolver gains
`contenders` + `skipped` from the start (cheap — the data is in hand at resolve
time), so the trace is never a retrofit. The dump UI lands with B-7.

### 11.6 Gates / invariants
- B0 (`test_merge_characterization`) + `test_parallel_pre_menu` stay byte-identical
  through B-5; only B-6 changes them, deliberately.
- B1 invariants (`test_merge_invariants`) become the resolver's C1/C2 regression gate.
- **B-4 flip gate (hard, 2026-06-22) — CLEARED 2026-06-23.** The two former
  strict-xfail divergences in `test_resolver_adapter.py` are both resolved, so the
  resolver may become the sole committer (B-4) without changing behaviour on an axis
  that was not consciously decided:
  - *duplicate meta track numbers* — **reproduce-today**: `meta_to_proposals` collapses
    to last-per-number (matches `_merge`'s last-wins `meta_by_num`). Behaviour-neutral.
    Now `test_equiv_duplicate_meta_track_number_last_wins`.
  - *invalid on-disc ISRC* — **uniform drop** (`resolver_adapter.sanitize_base`, user
    decision 2026-06-23): invalid ISRCs (R13) are dropped on every track at assembly,
    where `_merge` scrubs only MB-matched ones. A deliberate, documented divergence:
    the resolver agrees with `_merge` on the matched case and intentionally diverges on
    the unmatched case (resolver drops, `_merge` keeps garbage). Pinned by
    `test_invalid_isrc_dropped_uniformly_unmatched_DIVERGES_from_merge`; the matched
    case is `test_equiv_isrc_invalid_disc_matched_meta_empty_isrc`.
  - `sanitize_base` is the single committed-disc assembly contract — it MUST wrap the
    base at every **committed-disc assembly** (production shadow build + equivalence
    tests) so they agree; it is a no-op on a disc with no malformed ISRCs, and is *not*
    applied to bare unit tests of the `disc_from_resolution` primitive.
  - **Residual representational divergence on the same axis (documented, not a gate):**
    `baseline_proposals` normalises *every* valid on-disc ISRC, so an UNMATCHED track
    with a valid non-canonical (hyphenated/lowercase) ISRC resolves canonical where
    `_merge` keeps it raw (it normalises only matched tracks). Resolver-is-cleaner —
    same bucket as the falsy-but-present → None class. Pinned by
    `test_valid_noncanonical_isrc_normalised_uniformly_unmatched_DIVERGES_from_merge`.
    The two *enumerated* gate divergences are the ones that were resolved; this is a
    pre-existing representational one declared, not reconciled to byte-equality.
- **Equivalence is property-gated:** `test_property_resolver_equals_merge_on_clean_domain`
  (Hypothesis) fuzzes `(disc, meta)` over the live in-domain space (optional fields
  None-or-nonempty, valid/None ISRCs, unique track numbers, non-zero discogs id) and
  asserts `resolve == merge`. It found a third (representational) divergence on the
  first run; keep it as the regression gate against new field-interaction drift.
- `_pick_canonical_mcn`, the §10 rung, the AcoustID gate, the renderer: untouched.
- Run on min Python (3.10) per [[feedback_verify_min_python]]; ship via `scripts/sync.py`.

## 12. Discogs release selection by master (DECIDED and BUILT 2026-11-26)

kgr, 2026-11-26: *"The main problem is that the primary means of identification, after
the TOC-based MB DiscID (which only yields the release group), is the barcode, but the
exact match on Discogs often lacks a barcode."* This section replaces barcode-first
Discogs identification with **master-first narrowing by what the disc in hand shows**.
The post-menu chooser shipped the same day (`_discogs_after_menu` →
`_discogs_candidates_after_menu` → `_choose_discogs_release`, commits `3ebb8c6a`,
`dc6c7e74`, `1df61ce8`) was a **stopgap**, retired by this design the same day. Built in `menu_state.DiscogsMasterScreen` /
`DiscogsReleaseDetailScreen`, `discogs_lookup.master_countries` / `master_versions` /
`release_details` / `search_masters`, `mb_lookup.discogs_master_of_release_group`, and
`cdda2img._discogs_after_menu`; §12.5 lists where the build departs from the text below.

### 12.0 The case that forced it

Phil Collins, *No Jacket Required*, disc ID `DCvSBmF2Sn35OCvgXtnULUypFkw-`. Every
fact below was measured live on 2026-11-26.

- **MusicBrainz** holds four releases for the disc ID: two records of one German
  WEA pressing (`d73bd2e7`, `5302ea74`, barcode `022925169921`, XE), US Atlantic
  (`0136fad2`), and GB Virgin CDV 2345 (`0a19729e`, barcode `5012981234522`). The
  `barcode_plurality` rung narrowed the menu to the two German rows; it no longer
  narrows the menu (§10.3, `menu_candidates`).
- **The GB release has no Discogs link of its own.** Its release *group*
  (`86b4651b`) links Discogs **master 98041**. The MusicBrainz release page shows the
  group's links in its sidebar, which reads as the release's own.
- **The disc in the drive is Discogs 5222631**: "No barcode", 1985, "Manufactured in
  the UK", ADD on the back sleeve and AAD in the booklet. MusicBrainz's barcode for the
  GB pressing belongs to the '80s reissue ("Reissue of [r=5222631]. '80s reissue adds
  a barcode"), so a barcode search finds only 5221810 and 19343422 and can never reach
  the disc. 10759988 is a near-identical Nimbus pressing told apart only by SPARS codes.
- **The master's versions endpoint** (`/masters/98041/versions?format=CD`) returns
  108 CD versions and, in the same response, **facet counts**:
  country `US 36, Europe 18, UK 11, Japan 9, Canada 7, Australia 4, Brazil 4,
  France 3, Russia 3, …, UK & Europe 1, UK, Europe & US 1, Unknown 1`;
  released `1985 18, 1986 2, 1987 2, 1988 3, …, 2026 2`, which sums to **48 of
  108: 60 CD versions carry no year**, including 8 of the 11 UK CDs.
- **Discogs and MusicBrainz countries do not correspond one to one.** The German WEA
  pressing (MusicBrainz XE, "manufactured in Germany") is filed under Discogs
  "Europe"; "Germany" is not a facet of this master at all. Joint regions ("UK &
  Europe", "UK, Europe & US") exist.

### 12.1 Decisions (kgr, 2026-11-26)

**D1. Master first.** The Discogs candidates for a pressing are the versions of the
master that MusicBrainz links the release **group** to
(`mb_lookup.discogs_master_of_release_group`). The barcode is demoted from entry point
to a column and a hint: it is never a pre-selection, since the barcode MusicBrainz
holds for a pressing can belong to a reissue (§12.0).

**D2. CD only, on the server.** `format=CD` on the versions request. CD-R ("CDr"),
SACD and others are separate Discogs formats and are excluded by it.

**D3. Country is chosen by the user, from Discogs' own list.** The picker lists the
master's CD countries with their counts, exactly as the facet reports them, with the
settled MusicBrainz pressing's country pre-selected where it maps
(`countries.discogs_country_code` in reverse: GB → UK, XE → Europe and the joint
regions containing it) and one key to clear the filter. Free-text country entry is not
offered: a name Discogs does not use matches nothing.

**D4. Year is chosen by the user, after country, and NEVER pruned automatically.** The
picker lists the years the facet reports for the chosen country, plus `unknown (n)`.
**Choosing a year keeps the undated versions by default** (kgr: yes), with a toggle to
exclude them. An automatic "matches the MusicBrainz year" filter would treat "year
unknown" as "not that year" and drop 56% of this master's CDs, including near-duplicates
of the disc in the drive. A missing value is a third state, not a non-match.

**D5. A paged list, details on selection.** List rows come from the versions data alone
(id, country, year, label, catalogue number, format text): no request per row. Opening a
row fetches that one release (`discogs_lookup.release_details`, extended) and shows
**the barcode (or "not listed")**, every matrix/runout line, SPARS and other
identifiers, formats, label and catalogue number, the full notes, and the URL; [a]
applies it, any other key goes back. This replaces the stopgap's eager fetch of up to
20 releases before anything is shown.

**D6. A single barcode hit is a marked row in an interactive rip, not a decision**
(kgr: yes). When the settled pressing's barcode finds exactly one Discogs release, the
list shows it marked ("barcode matches MusicBrainz") and the user confirms or picks
another. On *No Jacket Required* an automatic one-hit rule would have taken a reissue had
Discogs held only one. Under `--auto` the one-hit rule stands (D7).

**D7. `--auto` records the master and relaxes the single-release rule** (kgr: yes).
Manual disambiguation is unavailable, and picking one of 108 releases by heuristic
records a pressing nobody checked. So, in order:
1. the MusicBrainz release's own Discogs release link, as now;
2. else exactly one Discogs release for the pressing's barcode naming the album, as now;
3. else **the master** MusicBrainz links the release group to, stored as a new
   identifier `discogs_master_id`. `discogs_release_id` stays **empty**: Discogs
   numbers masters and releases separately (98041 as a *release* is an unrelated
   record), so a master id in the release field would make every later
   `fetch_release` silently fetch the wrong album.

The archival gate (`_require_discogs_release`) becomes: **a Discogs release, or under
`--auto` a Discogs master**. An interactive rip still requires a release. Neither
`preferred_country` nor an earliest-year rule selects a Discogs release under `--auto`:
with D7.3 there is nothing for them to narrow. `release_date` keeps coming from the
MusicBrainz release the §10.3 ladder pinned; the release group's earliest year is
already `original_release_year`, and copying it into `release_date` would date every
reissue as the first pressing. `preferred_country` keeps its §10.2 role in the
MusicBrainz ladder.

**D8. Where it lives.** A screen in the metadata menu (`menu_state`), beside the
MusicBrainz pressing screen (`PressingScreen`), with the same paging (`[n]`/`[p]`)
and exits (`[s]` save, `[q]` discard, `[x]` none of these). It opens by itself after
the pressing screen when no Discogs release is settled, and stays reachable from the
menu to change a settled one (including one taken from the MusicBrainz link). Leaving it
with no release in an interactive rip aborts at `_require_discogs_release`, as now. The
post-menu prompt (`_choose_discogs_release`, `_discogs_chooser`) is deleted. The
existing Fetch → Discogs search screen stays as the free-form escape.

### 12.2 Edge cases

- **No master linked on the release group.** Interactive: offer a Discogs master search
  by artist and album (one confirmation, then D3 to D5 on the chosen master). `--auto`:
  abort (exit 3), since there is no release and no master to record. (kgr, 2026-11-26:
  agreed.)
- **Large masters** (a *Thriller* holds around a thousand CD versions). Country and year
  are applied as server-side filters (`country=`, `released=`), and the facets come with
  the first page, so a pick never pages through the whole master; the list pages only the
  survivors. `discogs_lookup._MASTER_VERSIONS_MAX_PAGES` then guards the filtered set,
  not the master.
- **Regions.** Pre-selection for MusicBrainz `XE` covers "Europe" and the joint regions
  containing it; `XW` pre-selects nothing. The user can always clear the filter.
- **Multi-disc sets.** A box set's Discogs release covers every disc; choosing it for
  disc *n* is correct, and nothing per-disc is stored from it. Unchanged from today.

### 12.3 Container and PROV

Spec first (`rbi_spec.md`), no format bump: PROV keys are optional.

| Key | Kind | Meaning |
|---|---|---|
| `discogs_master_id` | identifier | The Discogs master: the master the chosen release belongs to (its `master_id`), or under `--auto` the one MusicBrainz links the release group to when no release was settled (D7.3). |
| `discogs_release_id` | identifier | Unchanged meaning; **may be absent under `--auto`** when `discogs_master_id` is present. |
| `discogs_release` | observation | Gains `master_only:<master id>` (D7.3). |
| `discogs_release_via` | observation | Gains `master_versions` (picked on the D8 screen). `chosen` and `barcode_after_menu` retire with the stopgap; containers written in between keep them. |
| `discogs_master` | observation | `<id>:<shown>/<total>[@<country>][,<year>[+undated]]`: the master the screen opened, how many versions it listed of how many CD versions, and the filters in force when the user picked. |

`R11` (original-release corroboration) currently walks `release.master.main_release`;
with `discogs_master_id` known it can read the master directly, including under
`--auto` with no release. `list`, the catalogue and the RLOG name the master when there
is no release.

### 12.4 Build sequence

1. `rbi_spec.md` rows (done with this section), then `RBIDisc.discogs_master_id` and its
   PROV write and read.
2. `discogs_lookup`: `master_versions(master, *, country=None, year=None, page=)`
   returning the page and the facets; `release_details` gains every runout line and the
   other identifiers.
3. `menu_state`: the master screen (D3 to D6, D8), driven by fakes in tests and by a
   recorded *No Jacket Required* fixture (facets and versions as measured, §12.0).
4. `_discogs_after_menu` → the auto rules (D7); `_require_discogs_release` relaxed for
   `--auto`; stopgap deleted.
5. Live check: kgr re-rips *No Jacket Required* interactively (expects 5222631 under UK,
   1985 + undated) and once under `--auto` (expects `discogs_master_id=98041`, no
   release). `tools/archival_replay.py` on the written container, both ways.

### 12.5 As built (2026-11-26)

- **Unattended** means `--auto` **or** no terminal: the rule is "no human can be
  asked", which is what D7 is about. The screens never run without a terminal.
  Confirmed by kgr, 2026-11-26: running without a terminal is a deliberate act, so it
  gets the same outcome as an explicit `--auto`.
- **Leaving without a pick.** The screen has `[s]` (keep what is set) and `[q]` (put
  back what was there on entry); there is no separate `[x]`. An interactive rip that
  leaves with no release records `discogs_release=not_chosen` and aborts. The screen is
  not opened a second time for an unchanged pressing: it was already offered in the
  menu (D8).
- **Pressing changed in the menu, no MusicBrainz link.** The screen reopens on its own
  after the menu (`metadata_menu.run_discogs_menu`), with that pressing's single
  barcode hit marked if there is one.
- **What a pick changes** (kgr, 2026-11-26: "allow Discogs fields to overwrite MB
  fields, but only where they exist"). `discogs_release_id`, `discogs_master_id`,
  and each of `label`, `catalog_number`, `country`, `barcode`, `release_date` that
  the Discogs release states (`ReleaseDetails.catalogue_fields`); a blank never
  erases, and a Discogs region with no ISO code is left out. Recorded as PROV
  `discogs_overwrote`. On *No Jacket Required* 5222631 lists no barcode, so
  MusicBrainz's 5012981234522 (the reissue's) stays. Album, artist and tracks are
  not touched: they are album-level, and Discogs' artist names carry suffixes
  ("Phil Collins (2)") of their own.
- **`discogs_master_id` for a settled release** comes from the release itself (its
  `master_id`, one Discogs request when nothing set it), not from the release group's
  link: a release can sit under a different master from the one the group links.
- **R11 is unchanged**: it still reads the master through `discogs_release_id`, and
  runs before the menu, when an unattended master is not yet known. Reading
  `discogs_master_id` directly is a follow-up.
- **`--digital-media` lists the master's `File` versions** (kgr, 2026-11-26: "always
  use Discogs master's format=File versions for digital media, as anything else would
  be incorrect"), on the in-menu screen and the post-menu reopen alike; unattended,
  the master is recorded as for a pressed disc. Measured: Toca's master 51867 has two
  File versions, one of them its own digital release 22970945.
- **Confidence** (kgr, 2026-11-26: the user holding the disc is "always the strongest
  signal"). `match_distance.final_match_distance` already stored 1.000 / `strong` for
  a pressing picked by hand (`release_selection=manual`); a Discogs release picked on
  the master screen now counts too, when MusicBrainz offered one pressing or none, but
  not beside an un-reviewed `auto_tiebreak` pressing, which would still be a guess.
  After an interactive menu the stored grade is printed ("Metadata after your
  review"); before, only the pre-menu hint was ever shown.
- **A pick is credited only while the disc still carries it**: a release applied
  afterwards from Fetch → Discogs, or a `[u]` reset, is not recorded as
  `master_versions`, and a master the pick left behind is cleared.
- **`tools/archival_replay.py`** now runs the unattended post-menu step too. Replayed
  2026-11-26: ABBA *Gold* (master 12249), Tracy Chapman (105802), Eliminator (48605)
  and Toca under `--digital-media` (51867) all pass, each with its release.
- **Renderers**: `discogs_master_id` is a PROV key; `list`, the catalogue and the RLOG
  were not changed to name it.

## 13. Matching MusicBrainz and Discogs releases field by field (DECIDED and BUILT 2026-11-26)

kgr, 2026-11-26: *"The problem we're dealing with here is sloppy submissions, with little
to no auditing. So now we're forced to grab missing data from multiple sources, and try
to match it to a single product."* §12 settled *which Discogs release* a disc is; this
section pairs it with *which MusicBrainz pressing*, and fills each side's gaps from the
other.

### 13.0 The cases that forced it

- **Dire Straits, *Brothers in Arms*** (disc ID `fiOAciPzXBMSgC_0Fm.AZ4V3tKg-`, measured
  2026-11-26): five MusicBrainz pressings. Four (5a5330cd, 584fc120, 7f90585e,
  aa230c42) share barcode `042282449924` and catalogue number `824 499-2`; only
  aa230c42 carries a date (1986). What separates them is free text ("made in France by
  PDO, BIEM / STEMRA" against "…PMDC" and "…GEMA"). The pinned and the picked
  pressing both had no date, so the "Original" line, gated on the disc's own year,
  read "Unknown, unknown release (unknown year)" although the release group's first
  release (1985) was known.
- ***No Jacket Required*** (§12.0): MusicBrainz gives the GB pressing barcode
  5012981234522; the disc kgr held, Discogs 5222631, lists none.

### 13.1 Principles (kgr, 2026-11-26)

1. **A contradiction rejects.** Two present, normalised values that disagree mean the
   two records are not the same product.
2. **Absence is silent.** An empty field, or one claiming no data exists ("no
   barcode"), neither confirms nor refutes. kgr: *"'no barcode' must always be
   interpreted as 'no barcode submitted'."*
3. **Match what exists; adopt what is one-sided.** A value present on one side only is
   taken over, provided enough other present fields agree (§13.3).
4. **The floor is the album.** The disc ID identifies a MusicBrainz release *group*
   (its pressings share it), and that group links a Discogs *master*. In some cases
   that is all `--auto` can ever resolve, and the master may be missing too.
5. **Ask only for what cannot be settled.** The manual path asks the user for whatever
   the field match leaves ambiguous, and nothing else.

### 13.2 Fields and normalisation

| Field | Normalised as | Contradiction when |
|---|---|---|
| barcode | GTIN-13 (`barcode.normalize_barcode`) | both present and different |
| catalogue number | case folded; spaces, hyphens, dots, slashes removed (`824 499-2` = `8244992`) | both present and different |
| label | case folded; trailing "Records", "Ltd", "Inc", "GmbH" and punctuation folded | **never** (supporting only, as built 2026-11-26): the disc-ID response keeps MusicBrainz's first label of several (*Brothers in Arms* 5a5330cd: Phonogram and Vertigo, kept as Phonogram), and both services put label, imprint or distributor there interchangeably |
| country | MusicBrainz code against Discogs name, by **compatibility**, not equality: XE is compatible with any European country and with "Europe" and the joint regions containing it; XW and Discogs "Unknown" are absent | the two name places that cannot contain each other (GB against US) |
| year | compared at the precision both have | both present and different |
| format | CD (or File under `--digital-media`, §12) | different |
| track count | the disc's TOC against the release's tracklist | different |

### 13.3 What is "sufficient" (kgr: "we'll try your recommendation")

A pair matches when **a strong identifier, barcode or catalogue number, is present on
both sides and agrees**, and **no field contradicts**. Country, year, label, format and
track count support a match but cannot make one on their own: they are shared by too
many pressings.

kgr expects this to be too strict in practice (*"releases that only have a barcode and/or
catalogue number on one side but not the other"*), and names the fallbacks to add when it
is: **album title, total tracks, artist name**, and whatever else is needed. Build the rule
as a list of evidence classes with one threshold, so a fallback is an added entry and not a
rewrite, and record in PROV which fields matched, so the misses show which fallback to add.

### 13.4 Pairing, both ways

- **Candidates.** MusicBrainz: the disc ID's pressings. Discogs: the master's versions in
  the disc's format (§12).
- **Order of evidence.** (a) The MusicBrainz pressing's own Discogs release link.
  (b) The reverse link: MusicBrainz's URL lookup (`/ws/2/url?resource=<discogs release
  URL>&inc=release-rels`) names the MusicBrainz releases linking a Discogs release.
  (c) The field match (§13.3).
- **Rate limit.** The versions list carries catalogue number, label, country and year at
  no cost; a barcode needs one release fetch (60 a minute). Match on the free fields
  first and fetch details only for the survivors.

### 13.5 `--auto` (and any run without a terminal, §12.5)

- **Exactly one compatible pair:** take it, and fill each side's gaps from the other.
- **Otherwise: the release group only.** kgr: *"record the release group only and drop
  mb_release_id, as we have no way of corroborating the truth."* `mb_release_id` is not
  written; `mb_release_group_id` and the Discogs master (`master_only`, §12) are. A disc
  ID that matches a single pressing keeps it, since nothing was chosen between.
  **Changes every multi-pressing `--auto` rip**: today they record the ladder's guess as
  `release_selection=auto_tiebreak`.
- **No Discogs master either:** OPEN (§13.9 Q1).

### 13.6 The manual path

- Pick the pressing (§10.3 menu); the Discogs screen (§12) then lists only versions
  **compatible** with it, ranked by how many fields agree, and pre-selects its country.
- Or pick on Discogs first: the reverse link, else a unique field match, selects the
  pressing.
- The user is asked only where more than one candidate survives on a side.

### 13.7 Filling one side from the other

- A value present on one side only is adopted for the pair, and PROV records its source.
- **Barcode** (kgr: recommendation accepted): a barcode adopted from the partner is kept in
  PROV but never becomes the TOC's MCN. `_finalize_identifiers` synthesises `CATALOG`
  from a barcode only when that barcode was stated by a release the user picked, or by the
  pressing-level MusicBrainz release itself; otherwise a CD-R burned from the RBI would
  carry an MCN the original may not.
- **Conflicts** cannot occur within a matched pair (a contradiction rejects the pair).
  Between the user's pick and a value from elsewhere, the pick wins.

### 13.8 The original release

kgr: *"I'd prefer to abort rather than have the 'original' PROV blank or incomplete."* A
rip whose original release cannot be determined, with both a title and a year, aborts
(exit 3; 2 when the lookup failed on an outage). Sources: the MusicBrainz release group's
first release date, then the Discogs master's year as a fallback source (today it only
corroborates, R11). Also record **why** the release-group answer was rejected (R3 gate:
`track_count`, `durations`, `isrc`, `title_fuzz`): on *Brothers in Arms* the rip rejected
an answer that an exact replay accepted, and nothing recorded which gate fired.

Whether a disc whose own year is still unknown, after both sources are combined, also
aborts is OPEN (§13.9 Q2).

### 13.9 Ruled by kgr, 2026-11-26

- **Q1.** `--auto` and no Discogs master: **abort** (as before).
- **Q2.** This disc's own year unknown after both services: **the user is asked for it**,
  after the release selection. Unattended there is nobody to ask, so the rip aborts
  (consistent with "abort rather than incomplete"). kgr: *"We can always revisit some of
  these decisions later after testing."*

### 13.10 Build sequence

1. `rbi_spec.md` rows for the new PROV keys (matched fields per pair, adopted fields and
   their source, the R3 rejection reason), and the changed `mb_release_id` rule.
2. A pure matcher (`release_match.py`): normalisers, compatibility, contradiction,
   sufficiency, over plain records; tested on recorded *Brothers in Arms* and *No Jacket
   Required* data.
3. The reverse-link lookup (`mb_lookup`) and the detail fetch limited to survivors.
4. `--auto`: unique pair or release group only. Manual: the filtered Discogs list, and
   pressing-from-Discogs.
5. The original-release gate and the R3 reason.
6. `tools/archival_replay.py` over every container on disk before shipping, since the
   `--auto` change alters what multi-pressing rips record.

### 13.11 As built (2026-11-26)

- **Code.** `release_match.py` (pure: normalisers, `compare`, `matching_pairs`,
  `rank_compatible`, `unanimous`, record converters); `mb_lookup.mb_releases_linking_discogs`
  (MusicBrainz's `browse_urls`, 404 = no link); `discogs_lookup.master_original`;
  `cdda2img._settle_identity_after_menu`, which runs `_discogs_after_menu`, then
  `_pair_unattended` or `_pair_after_pick`, `_settle_discogs_master`,
  `_require_discogs_release`, `_settle_release_year` and `_require_identity_complete`;
  `metadata_menu.run_pressing_menu`; the Discogs screen's compatibility filter (`[m]`).
- **Label never contradicts** (§13.2, changed while building): the disc-ID response keeps
  MusicBrainz's first label of several, so *Brothers in Arms* 5a5330cd (Phonogram and
  Vertigo) looked contradicted by a Discogs "Vertigo" and a pressing was wrongly derived as
  unique. Label is supporting evidence only.
- **Release-group-only keeps a pressing field only when every candidate states it alike**
  (`unanimous(..., require_all=True)`): one dated pressing of seven (Tracy Chapman) does
  not date the album's disc.
- **The R3 duration gate was wrong for every disc with real pre-gaps.** MusicBrainz measures
  a track INDEX 01 to INDEX 01, so its length includes the next track's pre-gap; the gate
  summed TOC durations without them. On Tracy Chapman 17 s of gaps read as a mismatch
  against 3.85 s of tolerance, and the disc's own release was refused on Tracy, Eliminator,
  Toca and, very probably, *Brothers in Arms*; the fallback search hid it, until a
  MusicBrainz 503 did not. Fixed, with a regression test.
- **The on-disc MCN never enters the match** (kgr: a disc in his collection has an MCN
  unlike its barcode), and is never replaced by a barcode.
- **Replay, unattended** (`tools/archival_replay.py`, which now runs the original-release
  lookup and `_settle_identity_after_menu` with the config's `preferred_country`): ABBA
  *Gold* passes as release group only (every pressing 1992); Tracy Chapman and Eliminator
  abort for the disc's year, as ruled, but now take their original release from the
  release group (1988, 1983) instead of the fallback search; Toca (`--digital-media`)
  passes with its release. Toca is still refused on `durations`, and rightly: it is a CD-R
  made from the digital release, with no pre-gaps, whose tracks run about 0.7 s longer
  each than MusicBrainz's file lengths (11 s over 15 tracks against 5.25 s), so the
  search supplies its original (*Toca*, 2000).
- **A default left in place is a yes; "none of these" is a no** (kgr, 2026-11-26: *"If the
  user is presented a question, and leaves the answer at the default, we shouldn't second
  guess their reason, but should take their acceptance (or lack of rejection) as a
  positive response. This does however depend on us also offering the option 'My disc is
  none of the above'"*). Leaving the pressing screen with the marked pressing in place
  records `release_selection=accepted` (scored as user-confirmed), provided the marked one
  is a listed row. `[x]` now records the album only: the pressing, its fields and the
  Discogs release linked from it are dropped, no pressing is derived from a Discogs pick,
  and the master screen reopens for the user's Discogs pick.
