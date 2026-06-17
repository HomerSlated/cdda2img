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
