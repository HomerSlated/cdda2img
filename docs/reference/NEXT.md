# Add these to TODO.md, highest priority

- A future refactor moving normalize_barcode to a neutral module (e.g. barcode.py)
- Make the entire metadata menu a fixed position/redraw state machine
- Add the AccurateRip output to the state machine (pause on that page, before loading the metadata menu)
- Title-fuzz fallback for original_release lookup when MB has no release-group hit
  (algorithm fully specified in `private/research/incoming/original-release-detection.md`;
  needs the `rapidfuzz` dependency)
- Rewrite `docs/flow/data-model.md` to drop the v3-era `remastered_source` enum and
  document the new `low_dynamic_range` / `original_release_*` fields
- `docs/reference/TUI_Design.md` lines 49-50 still describe a `Master/Remaster` toggle
  — update to `Silence: trim / notrim`
