# Add these to TODO.md, highest priority

- A future refactor moving normalize_barcode out of `cdda2img.barcode`
  into a more general "validation" module if more EAN/UPC helpers
  accumulate. Today `barcode.py` is the single-function module
  carved out of `discogs_lookup.py`; it can grow if needed.
- Continue the metadata-menu state-machine rewrite: port the
  EDIT / FETCH / ORIGINAL_RELEASE sub-menus from their procedural
  inner loops to per-substate renderers under `menu_state.MenuState`.
  The top-level state machine + AR_PAUSE landed today; sub-menus
  still use the legacy nested-loop helpers from `metadata_menu.py`.
- Suppress the duplicate AR report print in `rip_image` once the
  AR_PAUSE state is the canonical display surface — the existing
  `print_ar_report` call writes to stdout and is immediately wiped
  by AR_PAUSE's screen-clear. Cheap to keep for now (batch / non-TTY
  mode still needs it); a refactor can route both paths through one
  helper.
