# Identifier trust & a learned per-(source, field) confidence model — EXPLORATORY

**Status:** captured 2026-06-27 from a design rumination. **Not decided.** Companion to
`trust_model_design.md` (which describes the *shipped* static-trust resolver, B1–B6).
Part 1 supersedes the fuzzy-MCN-match direction of the deferred MCN-gate fix
(`docs/reference/TODO.md`). Part 2 is an ambitious long-term vision; Part 3 is the honest
engineering assessment.

---

## 1. MCN ≠ barcode — separate the two fields (near-term, concrete)

**Empirical finding (Tracy Chapman s/t):** the on-disc Q-channel MCN `7559607740206` is the
human-readable catalogue number `7559-60774-2` plus an arbitrary `06`; the printed/scanned
barcode is `075596077422`. The MCN is unreferenced on MusicBrainz and Discogs. The earlier
assumption that *the MCN is a normalised barcode* is demonstrably wrong — they are
**different identifiers in different namespaces**.

**Consequence:** fuzzy-matching an on-disc MCN against service barcodes (the deferred
MCN-gate fix) is conceptually unsound — it matches two different things and will both
false-accept and false-reject. **Drop the fuzzy-match approach.** Replace with a clean
separation:

- **`mcn`** — the Q-channel Mode-2 / CD-Text UPC_EAN MCN. **Archival PROV only**, stored
  verbatim (this is an archival project). Used for identification **only as a desperate
  last resort** when no barcode is available — the §10.3 "barcode positively matches the
  on-disc MCN" rung survives as a *positive* last-resort signal, **never a veto**.
- **`barcode`** — the UPC/EAN that online services actually reference. The real
  disambiguation identifier, alongside the MB Disc ID and ISRCs. Sourced from MB / Discogs
  (the physical disc rarely carries a barcode distinct from the MCN).

**Bug this closes:** the Unit-G MCN veto (`mb_lookup._is_consistent`) discarded an *exact*
MB disc-ID match because an archival MCN disagreed with the catalogued barcode. An archival
MCN must **never veto a stronger identifier**. Removing the veto closes the live bug more
cleanly than demote-and-fuzzy-match.

**Why this is the right framing:** metadata services are the real means of identification —
we have no other. Disc-mastered metadata is rare, and when present it either conflicts with
the services or is unreferenced (like this MCN). So the disc's MCN earns archival storage
and last-resort use, nothing more.

**Format impact:** `RBIDisc` gains a `barcode` field distinct from the existing MCN field
(`catalog`). The `mcn`/`barcode` split threads the toc, PROV, and the §10 selection rungs.

---

## 1a. RESOLVED 2026-06-29 — the final, decided shape

The §1 direction is adopted **and tightened**. Two refinements settle it:

**(i) The MCN is archival-only, full stop — no disambiguation use *at all*.** Not a veto,
not a fuzzy match, not a low-trust confidence contribution. The earlier "keep MCN as a
positive last-resort signal" rung (§1, §10.3 `mcn`) is **dropped**. Rationale (decisive
point): distinct releases routinely carry barcodes differing by only a few digits, so a
fuzzy MCN match false-accepts genuinely-different releases — and the on-disc MCN of the
motivating disc is unreferenced even on Google. An identifier with no external references
cannot disambiguate; granting it *any* weight is a logical fallacy, not merely low value.

**(ii) Clone vs archive — the governing principle (retires the synth-MCN fidelity worry).**
A *clone* is barren and may be unidentifiable in future; an *archive* is the pristine
invariant (**raw PCM + track timing**) **supplemented** with research material — copied from
the source when present, sourced externally when blank. We *already* write externally-sourced
titles and ISRCs into the generated TOC; that is archival enrichment, not infidelity, and
the verbatim-clone standard was never our standard for the TOC's textual/identifier layer.
The supplemental data isn't false — it populates previously-blank fields. Therefore
synthesising a missing MCN is legitimate: the MCN field is *defined* to carry the UPC/EAN
(Q-ch Mode 2 = 13 BCD barcode digits), so deriving it from the disc's barcode fills the
field's own canonical content.

**Decided spec:**

- **MCN** (archival only): on-disc MCN present → TOC `CATALOG` line → burned, with
  `mcn_source=disc` in PROV. Absent → derive from the normalised barcode → TOC `CATALOG`
  → burned, with `mcn_source=barcode_derived`. The `mcn_source` marker lets a future
  reader (or our own re-rip) tell a genuine on-disc MCN from a reconstructed one **without
  changing how the MCN is used** (it is never used beyond archival) — an early single-field
  instance of the §11.5 per-field traceability.
- **Barcode** (the disambiguation key): new `RBIDisc.barcode`, sourced from MB/Discogs,
  added to the B1–B6 resolver as a weighted signal (barcode-vs-barcode, same namespace).
  Persisted in **PROV only** — not the TOC, not the physical layer.
- **Veto:** remove the Unit-G cross-namespace MCN veto (`mb_lookup._is_consistent`). The
  per-track ISRC veto stays (exact, same-namespace, sound).

**No RBI format/version bump.** `build_prov_block` serialises an arbitrary `key=value`
dict; the binary layout is independent of the PROV key set (precedent: `discogs_release_id`
is an `RBIDisc` field persisted *only* to PROV; `recovery_track_<n>` was added at v6.0 with
no bump). "Spec-before-code" here means **documenting** the new `barcode` / `mcn_source`
keys in `rbi_spec.md` + CLAUDE.md — not a `VERSION_MINOR` change.

**Two-phase implementation — ALL DONE (2026-06-30):**
- **Phase 1 (DONE, 8ef6c2b)** — removed the `_is_consistent` MCN veto. Closes the live Tracy
  Chapman bug (an exact disc-ID match was discarded because an archival MCN disagreed with a
  catalogued barcode). Pure resolver logic, independently testable.
- **Phase 2 step 2+4 (DONE, 9116c20)** — added `RBIDisc.barcode` + resolver `Field.BARCODE` +
  MCN-from-barcode synthesis (`_finalize_identifiers`) + `mcn_source` marker. The on-disc MCN
  no longer seeds a lookup (no carve-out) and is no longer a *commit* key.
- **Phase 2 step 5 (DONE)** — tore out the remaining MCN *disambiguation* machinery
  (`_disambiguate_by_mcn`, the §10.3 `mcn` rung, the `mcn_hits` subset narrowing, and
  `barcode.mcn_matches`). Release selection now rests entirely on the candidates' own service
  barcodes (`barcode_plurality`) — a same-namespace comparison. The American Idiot
  TOC-collision case is preserved through that sounder mechanism. End-state of §1a reached: the
  on-disc MCN is read for no lookup, no veto, no ranking — archival only.

---

## 2. The grand vision — learned per-(source, field) confidence (long-term)

Generalise the *static* trust ladder (B1–B6) into a *learned* one: a confidence ≈
"probability of truth" per field, refined from the user's own corrections.

### 2.1 Identifier taxonomy (formal groundwork)

Enumerate every identifier, and for each define: **what it is**, **where to find it**, **how
to cross-reference it**, **how to validate it**, its **standalone validity**, and its
**validity relative to competing sources**:

| Identifier | Where | Validates against | Standalone strength |
|---|---|---|---|
| MB Disc ID | computed from TOC (SHA-1) | MB release lookup | very high (geometric disc identity) |
| ISRC | Q-ch Mode-3 / MB / CD-Text | per-track, MB recording | high (per-recording) |
| Barcode (UPC/EAN) | MB / Discogs (printed) | GS1 check digit; cross-DB | high (per-release) |
| MCN | Q-ch Mode-2 / CD-Text UPC_EAN | GS1 check digit | low (often unreferenced — §1) |
| AcoustID | Chromaprint per track | MB recording | medium (per-recording, fuzzy) |
| CD-Text | R-W subchannel | — | medium-high when present (absence is the problem, not wrongness) |
| Catalogue # / label / country | Discogs / MB | cross-DB | medium (Discogs > MB per B-6) |
| TOC / durations | the disc itself | duration-match (stage 7) | medium (collision-prone) |

Compose these into a per-field **confidence level** rather than a fixed trust rank.

### 2.2 The learning core

- Capture the resolver's automatic deductions **pre-menu** vs the user's accepted values
  **post-menu** (this is exactly the §11.5 traceability record: `contenders` + the winner
  per field).
- For each field, record which source's value (the "winner") **agreed or disagreed** with
  the user's final value.
- Accumulate a per-`(source, field)` **trust score** over many discs (e.g. "Discogs gets
  the label right 87 % of the time"), persisted across runs.
- Feed the learned scores back as the resolver's per-`(source, field)` trust — a
  self-refining loop. **Skipped under `--auto`** (no human signal that pass).

**Architectural fit:** this is the natural successor to **B-7** + **§11.5** — the resolver
already retains the full contender set per field; the learning core is "persist the
pre/post diff and update a prior." `build_match_distance` is the scoring seam.

---

## 3. Engineering assessment (caveats / pushback)

**Endorse Part 1 unreservedly.** The MCN/barcode split is correct, closes the live bug, and
is cleaner than demote-and-fuzzy-match. It should replace the deferred MCN-gate item.

**Part 2 is sound in direction but has real traps:**

1. **Sample size.** Per-user, per-`(source, field)` counts stay tiny for a long time.
   "87.4 %" implies precision you won't have from a handful of discs. Use **Bayesian priors**
   (start from the B1–B6 static ladder as the prior; update slowly) with **credible
   intervals**, not raw frequencies — and let confidence *widen* the model's uncertainty,
   not just point-estimate a percentage.
2. **The ground-truth fallacy (again).** "Post-menu = truth" is the same trap flagged in
   [[project_metadata_authority_model]]: the user is the best *available* arbiter, not an
   oracle. The learning signal is "agreement with the user's accepted value," which is
   weaker than truth, and **biased by what the user bothered to check** — an un-corrected
   field is not confirmed-correct, merely un-inspected. Don't treat silence as a positive
   label; weight inspected-and-kept higher than untouched.
3. **Hidden confounders.** Provider accuracy varies by **genre / era / region**, not just by
   field (Discogs strong on electronic; MB on classical). A single global per-`(source,
   field)` score is a coarse first cut; segmenting comes later, if ever.
4. **Cost/benefit** ([[feedback_metadata_over_engineered]]). The payoff concentrates in
   `--auto` (consistent with the OPT-4 finding) and in default menu ordering; interactive
   users correct anyway. Weigh the machinery (persistent stats store, diff capture, careful
   small-sample statistics, cold-start) against that bounded payoff before building.

**Recommended sequencing:** Part 1 (MCN/barcode split) is a concrete near-term refinement —
do it on its own. Part 2 rides on B-7 + §11.5 (the traceability surfaces are its data
source), so it cannot precede them; treat it as the *learned* evolution of the resolver once
those land, and prototype the statistics on logged pre/post diffs **before** wiring it into
the live trust ranks.
