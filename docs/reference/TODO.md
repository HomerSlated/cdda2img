# TODO

## Open

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

**Unified with OPT-4 in `docs/reference/trust_model_design.md` (2026-06-17)** — the
collect→resolve trust model closes C1 (physical fields proposed at `OBJECTIVE` by one
producer, resolved via one `replace`-based assembler) and C2 (recording-level sources'
proposal schema omits disc-level `mb_release_id`) by construction. Decision §5.2 there
is exactly this item's "typed API vs. chokepoint + invariant test" choice.

### Remaining metadata-pipeline work (2026-06-15)

Sources: bug-hunter `private/bugs/2026-06-15_163056_metadata-pipeline.md`,
optimiser `private/optimiser/2026-06-15_metadata-consensus.md`.
BUG-1..7, OPT-1/2/3 and the superseded P3 are complete — archived in the DONE
log below. OPT-4 is the only live item from this audit.

- [ ] **OPT-4** · **Per-field trust score model** — The current fill-blank / first-writer-wins
      model lets a wrong-but-non-blank CD-Text or CDDB value permanently block a stronger MB
      value; the only escape is the interactive menu's "Overwrite All". The recommended fix is an
      explicit `(field, value, trust)` proposal model: each source proposes a trust level per
      field, the highest-trust proposal wins, and near-ties surface as alternatives in the menu.
      Extend the existing `match_distance` / `build_match_distance` scaffold rather than adding a
      new framework. This is a substantial rework; design before implementing.
      **Design proposal drafted 2026-06-17: `docs/reference/trust_model_design.md`** —
      unifies OPT-4 with the Structural C1/C2 item below (collect→resolve, per-(source,field)
      trust). Awaiting a scope decision (§5) before any code.

---

### Minor / pre-existing

- **AcoustID `_chain_to_mb` reads an empty release-group stub.** On the recording
  endpoint, `inc=releases` does *not* embed the release-group's fields, so
  `rg.get("id")` and `rg.get("first-release-date")` are always None on the AcoustID
  path — `mb_release_group_id` and `original_release_date` have never populated there.
  (Confirmed live 2026-06-09 while fixing the invalid-include regression.) Low impact:
  the full-release fetch on select recovers this via the release endpoint. Fix would
  need a per-release follow-up call — deferred (same per-row cost we declined for Trk).

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

### Delivery mode audition (WIP — `src/cdda2img/audition.py`)

The loudness processing level is not user-selectable. The standard is fixed at −18 LUFS
(ReplayGain 2.0 / ITU-R BS.1770-3) and the only delivery choices are:

- **Unprocessed** — no loudness adjustment; clean archival audio
- **Normalised** — EBU R128 at −18 LUFS, audio modified, no tags
- **ReplayGain** — unmodified audio with REPLAYGAIN_* Vorbis tags; player applies gain

The audition tool allows the user to compare all three on the loudest 10-second passage
before committing. It is implemented as `src/cdda2img/audition.py` (run with
`uv run python -m cdda2img.audition <file>`). TUI integration is not planned — the
standalone CLI module is the intended form.

- [x] Find loudest 10-second window (peak-frame centring via PyAV + numpy)
- [x] Extract clip and prepare all three variants (PyAV + FFmpegNormalize + pyebur128)
- [x] Embed REPLAYGAIN_* tags in the RG variant (mutagen in-place patch via `replaygain.embed_rg_tags()`)
- [x] Interruptible looping playback (ffplay subprocess, SIGSTOP/SIGCONT for pause)

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
- Delivery mode audition panel (compare unprocessed / normalised / ReplayGain before
  committing to extract; see `src/cdda2img/audition.py` for the standalone prototype)


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
