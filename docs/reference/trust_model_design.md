# Design proposal — per-field trust model (OPT-4 + Structural C1/C2)

Status: **DRAFT — design only, awaiting decision.** No code until scope is approved.
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

## 5. Decision points (need answers before Phase A)

1. **Scope.** Full collect→resolve (Phases A–C) — the clean fix, larger change — vs. a
   lighter "trust-tagged overwrite" that only lets a higher-trust source overwrite a
   lower-trust *non-blank* field, keeping the imperative `_merge_into_disc` structure.
2. **Enforcement of C1/C2.** Typed proposal schema (a source can only emit its allowed
   fields — strongest) vs. documented chokepoint + invariant tests (the Structural item's
   lighter alternative).
3. **Trust source.** Fixed enum table in code vs. tunable via `Config`.
4. **Menu alternatives.** In-scope for the first cut, or deferred to a later pass?

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
