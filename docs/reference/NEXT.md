# Add these to TODO.md, highest priority

- **Deferred from metadata-pipeline R-recommendations pass (2026-05-28):**
  - **R7 extras:** add the three remaining cache tables —
    `isrc_lookups` (infinite TTL — ISRC mappings are immutable in practice),
    `discogs_barcode` (30-day TTL), `cddb_lookups` (30-day TTL). Schema
    follows the `disc_id_lookups` pattern already in
    `src/cdda2img/lookup_cache.py`.
  - **R8 parallel pre-menu MB+CDDB:** restructure `rip_image` to move
    CDDB into `_finalize_import`, wrap both prepops in a
    `concurrent.futures.ThreadPoolExecutor` of two workers, merge in
    CDDB-first → MB-second order with non-blank-wins semantics. Failure
    isolation is the primary benefit (a flaky CDDB shouldn't block MB).
- A future refactor moving normalize_barcode to a neutral module (e.g. barcode.py)
- Make the entire metadata menu a fixed position/redraw state machine
- Add the AccurateRip output to the state machine (pause on that page, before loading the metadata menu)
- Create a bug-hunter agent: Looks for silent failures, unexpected results, semantic errors, and logical fallacies, in addition to the usual sorts of bugs that would cause crashes, data corruption, memory leaks, etc.
- Rewrite `docs/flow/data-model.md` to drop the v3-era `remastered_source` enum and
  document the new `low_dynamic_range` / `original_release_*` fields
- `docs/reference/TUI_Design.md` lines 49-50 still describe a `Master/Remaster` toggle
  — update to `Silence: trim / notrim`
