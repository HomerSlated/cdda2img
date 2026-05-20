# LINT.md — Lint Override Register

This file documents every `# type: ignore`, `# noqa`, per-file ruff ignore, and
intentional underscore-prefixed unused variable in the codebase. Each entry has a
unique UID that is cross-referenced in the source code comment so reviewers can find
the rationale without guessing.

Format per entry:
- **UID** — stable identifier, never reused
- **Rule** — ruff code or ty error name
- **Location(s)** — file:line
- **Rationale** — why the suppression is correct
- **Alternatives** — any alternatives considered, with disposition

---

## LINT-001 — OR-Tools CP-SAT: missing type stubs

- **Rule:** `ty: attr-defined`
- **Locations:**
  - `input_selector.py:65–68` (`_knapsack_single_disc`, `bech` model)
  - `input_selector.py:121–122, 125, 128–129, 131, 135, 137` (`batch_ball` model)
- **Rationale:** The Google OR-Tools Python package (`ortools`) does not distribute `.pyi`
  stub files, and no `py.typed` marker is present. Every method call on `CpModel` and
  `CpSolver` instances — `.NewBoolVar()`, `.Add()`, `.Maximize()`, `.Minimize()`,
  `.Solve()` — is therefore flagged as an undefined attribute by `ty`. The code is
  correct; this is purely a stub coverage gap.
- **Alternatives:**
  - *Write project-local stubs for the OR-Tools methods we call* — feasible, but stubs
    would need updating whenever `ortools` is upgraded. The CP-SAT API is stable but
    the effort is disproportionate for a prototype.
  - *Use `cast(Any, model)`* — does not help; the error fires on each method call, not
    on the object assignment.
  - *Contribute stubs to the `ortools` package or `typeshed`* — the correct long-term
    fix, but external to this project.
- **Decision:** `# type: ignore[attr-defined]` on each call site is correct and minimal.
  The OR-Tools calls are well-tested by the `bech` and `ball` strategy integration.

---

## LINT-002 — PyAV AudioResampler: over-broad frame union in stubs

- **Rule:** `ty: invalid-argument-type` / `arg-type`
- **Locations:**
  - `audition.py:55` — `resampler.resample(frame)` in `_find_peak_window()`
  - `replaygain.py:110` — `resampler.resample(frame)` in `_decode_interleaved()`
- **Rationale:** `packet.decode()` is typed in the PyAV stubs to yield the broad union
  `AudioFrame | VideoFrame | SubtitleSet`. `AudioResampler.resample()` only accepts
  `AudioFrame | None`. In both call sites the stream being demuxed is an audio stream
  (`c.streams.audio[0]`); decoding an audio packet can only produce `AudioFrame` objects.
  The stubs are over-broad — they cannot express the stream-type constraint — so the
  error is a false positive.
- **Alternatives:**
  - *`isinstance(frame, av.AudioFrame)` guard* — **rejected**. This creates a branch
    that is dead code in practice: on an audio stream the condition is always True.
    Introducing dead branches obscures logic and could mask future bugs (e.g. if stream
    type changes, the frame would be silently dropped rather than erroring).
  - *`cast(av.AudioFrame, frame)`* — a zero-runtime-cost cast would satisfy `ty` at the
    assignment but the type mismatch error fires at the `resample()` call, not the
    assignment, so `cast` does not help here without also suppressing at the call site.
  - *Contribute a narrowed overload to PyAV stubs* — correct long-term fix; the
    `streams.audio[0]` return type could carry a phantom type parameter that narrows
    `decode()`. External to this project.
- **Decision:** `# type: ignore[arg-type]` is correct. The comment on each suppression
  documents the reasoning inline.

---

## LINT-003 — PyAV `add_stream(template=)`: missing stub overload — RESOLVED

- **Rule:** `ty: no-matching-overload` / `call-overload`
- **Former location:** `replaygain.py:244` — `out_c.add_stream(template=in_stream)`
- **Resolution:** PyAV 16.0.1 removed `template=` support from `add_stream()` entirely
  (the Python API no longer accepts it; the Cython signature is `add_stream(codec_name, rate=None)`).
  `embed_rg_tags()` was rewritten to use mutagen instead: `mutagen.flac.FLAC` patches the
  Vorbis comment block in-place without any audio re-encoding, which is both simpler and
  correct. The `add_stream(template=)` call and its `# type: ignore[call-overload]` are
  gone entirely.

---

## LINT-004 — mutagen: no type stubs distributed

- **Rule:** `ty: import-untyped`
- **Location:** `metadata.py:7` — `from mutagen import File`
- **Rationale:** The `mutagen` package does not include a `py.typed` marker and distributes
  no `.pyi` stub files. It is therefore treated as an untyped import by `ty`. The package
  is the de-facto standard for audio file metadata in Python and has no typed equivalent
  with comparable format support (MP3/FLAC/M4A/OGG/WMA/AIFF).
- **Alternatives:**
  - *`mutagen-stubs` on PyPI* — exists but is incomplete, unmaintained, and lags the
    main `mutagen` releases. Adding an incomplete stub dependency is worse than the
    suppression.
  - *Write project-local stubs for `mutagen.File`* — `mutagen.File` returns a dynamic
    type depending on the file format, making accurate stubs non-trivial. Disproportionate
    for a prototype.
  - *Switch to a typed alternative* — no alternative library covers the same set of
    formats with equivalent quality.
- **Decision:** `# type: ignore[import-untyped]` is correct.

---

## LINT-005 — Module-level compile-time struct size assertions (`rbi_format.py`)

- **Rule:** `ruff: S101` (use of `assert`)
- **Locations:**
  - `rbi_format.py:104` — `assert HEADER_STRUCT_SIZE == HEADER_FIXED_SIZE`
  - `rbi_format.py:113` — `assert RG_BLOCK_FIXED_SIZE == 17`
- **Rationale:** S101 exists to flag assertions used as input validation, which can be
  silently stripped by `python -O`. These assertions are different: they verify at
  module-load time that `struct.calcsize()` matches the expected constant. If anyone
  edits `HEADER_STRUCT` or `RG_BLOCK_FIXED_STRUCT` (the format strings), the error fires
  immediately on `import cdda2img.rbi_format` — not buried in a later write path. This is
  the correct place for this check: the struct definition and its size invariant live in
  the same module and should be co-located.

  Stripping these with `-O` would not cause a security issue; it would cause corrupt RBI
  files on the next write, which would be caught immediately on read. We do not run with
  `-O` in production or CI.
- **Alternatives:**
  - *`if HEADER_STRUCT_SIZE != HEADER_FIXED_SIZE: raise RuntimeError(...)`* — avoids S101
    but adds two lines of noise per invariant without any benefit. The `assert` is
    semantically precise: this is an invariant, not error handling.
  - *Move to a `pytest` test* — the invariant is about the module definition itself, not
    runtime behaviour. It belongs in the module, not in the test suite.
- **Decision:** `# noqa: S101` is correct. These are compile-time invariant guards, not
  security-critical input validation.

---

## LINT-006 — Struct size integrity check before disk write (`container.py`)

- **Rule:** `ruff: S101`
- **Location:** `container.py:169` — `assert len(header) == HEADER_FIXED_SIZE`
- **Rationale:** After `struct.pack(HEADER_STRUCT, ...)`, this assertion verifies that
  the packed bytes are exactly `HEADER_FIXED_SIZE` before writing them to disk. If the
  pack produced the wrong number of bytes — which can happen if `HEADER_STRUCT` is
  accidentally changed without updating `HEADER_FIXED_SIZE` — the entire RBI file would
  be silently corrupt at the header level, making it unreadable. The assertion is a last
  line of defence at the write boundary.

  Same `-O` reasoning as LINT-005 applies.
- **Alternatives:**
  - *`if len(header) != HEADER_FIXED_SIZE: raise RuntimeError(...)`* — marginally more
    `-O`-robust but adds noise. The struct size is already verified at module load by
    LINT-005; this assertion is belt-and-suspenders for the write path specifically.
- **Decision:** `# noqa: S101` is correct.

---

## LINT-007 — Caller-contract logic invariant (`replaygain.py`) — RESOLVED

- **Rule:** `ruff: S101`
- **Former location:** `replaygain.py` — `assert state is not None  # noqa: S101`
- **Resolution:** Suppression removed. `_measure_concat()` now opens with an explicit
  boundary guard:
  ```python
  if not paths:
      msg = "_measure_concat() requires at least one path"
      raise ValueError(msg)
  ```
  The loop was also refactored: `state` is now initialised unconditionally from
  `paths[0]` before the loop iterates `paths[1:]`, so `ty` can prove `state` is always
  a `pyebur128.R128State` at `_state_results()` without any guard or suppression. The
  `assert` and its `# noqa` are gone entirely.

---

## LINT-008 — Trusted internal subprocess call for `ffplay` (`audition.py`, `track_preview.py`)

- **Rule:** `ruff: S603` (subprocess without `shell=True` check)
- **Locations:**
  - `audition.py:186` — `subprocess.Popen(cmd, stdin=subprocess.DEVNULL)` in `Player.play()`
  - `track_preview.py:89` — `subprocess.Popen(cmd, ...)` (looping `ffplay`) in `_grab_and_play()`
- **Rationale:** S603 warns that subprocess calls may execute untrusted input. Here `cmd`
  is constructed entirely from hardcoded constants and resolved `Path` objects inside the
  module — no user-supplied string ever reaches subprocess arguments. The `ffplay`
  invocation is for the `audition.py` looping audio playback feature, where `ffplay` is
  a trusted binary on the user's `PATH`.
- **Alternatives:**
  - *Replace `ffplay` subprocess with `sounddevice` + `soundfile`* — this is the correct
    long-term fix, tracked in the TODO as call 6 of the subprocess elimination plan.
    A `sounddevice` callback player would remove this subprocess entirely, eliminating
    the S603 concern and the `ffplay` binary dependency. Deferred until the TUI
    integration phase.
  - *Validate each element of `cmd`* — unnecessary; all elements are internal constants.
    Validation would be security theatre with no benefit.
- **Decision:** `# noqa: S603` is correct for now. Will be eliminated when `Player` is
  ported to `sounddevice`.

---

## LINT-009 — S101 per-file ignore for tests (`pyproject.toml`)

- **Rule:** `ruff: S101`, suppressed project-wide for `tests/*`
- **Location:** `pyproject.toml` → `[tool.ruff.lint.per-file-ignores]`
- **Rationale:** `pytest` uses `assert` as the canonical testing idiom. S101 in test files
  is universally suppressed across the Python ecosystem for this reason — the rule's
  "assert can be stripped by -O" concern does not apply to test code, which is never run
  with optimisations enabled.
- **Alternatives:** None meaningful. Any alternative (e.g. `self.assertEqual`) is
  non-idiomatic for pytest and adds noise.
- **Decision:** Correct. This is standard Python project configuration.

---

## LINT-011 — tomli compatibility shim for Python < 3.11 (`config.py`)

- **Rule:** `ty: import-not-found`, `ty: no-redef`
- **Location:** `config.py:14–16`
- **Rationale:** `tomllib` is part of the Python standard library from 3.11 onwards.
  For Python 3.10 (the project's minimum), `tomli` is listed as a conditional dependency
  (`tomli>=2.0.0 ; python_version < '3.11'`) and provides an identical API.
  The standard compatibility shim is:
  ```python
  try:
      import tomllib
  except ImportError:
      import tomli as tomllib
  ```
  `ty` is configured for Python 3.13 (`python-version = "3.13"` in `pyproject.toml`),
  so the `except ImportError` branch is statically unreachable. `import-not-found` fires
  because `tomli` is a conditional dependency absent from the 3.13 virtual environment;
  `no-redef` fires because `tomllib` is bound twice in the same scope.
- **Alternatives:**
  - *`sys.version_info >= (3, 11)` branch* — functionally equivalent; `ty` may still flag
    the unreachable else branch. Slightly less idiomatic than try/except for this pattern.
  - *Require Python ≥ 3.11* — eliminates the issue entirely. Deferred; the project still
    targets 3.10 for tox CI coverage.
- **Decision:** `# type: ignore[import-not-found,no-redef]` is correct. This is the
  canonical PEP\~508 / `importlib` compatibility pattern used across the Python ecosystem.

---

## LINT-012 — Trusted internal subprocess calls for `cd-paranoia` (`disc_reader.py`, `track_preview.py`)

- **Rules:** `ruff: S603` (subprocess without `shell=True` check), `ruff: S607` (partial executable path)
- **Locations:**
  - `disc_reader.py:63` — `subprocess.run(["cd-paranoia", "-Q", ...])` in `query_disc()`
  - `disc_reader.py:168` — `subprocess.run(cmd)` (`cd-paranoia` rip) in `rip_disc()`
  - `track_preview.py:112` — `subprocess.Popen(cmd)` (`cd-paranoia` track-1 grab) in `_grab_track1()`
- **Rationale:**
  - *S603:* Both calls construct argument lists entirely from hardcoded string literals,
    the caller-supplied `device` string (a device path like `/dev/sr0`), and a resolved
    `Path` for the output WAV. `shell=False` (the default) is used throughout, so there
    is no shell injection surface.
  - *S607:* `"cd-paranoia"` is intentionally a name lookup via `PATH`, not an absolute
    path. `cd-paranoia` is a standard system package (`cdparanoia` on Debian/Ubuntu) with
    no known impersonation risk in normal CD-ripping environments. Hardcoding
    `/usr/bin/cd-paranoia` would break on systems where it is installed elsewhere.
- **Alternatives:**
  - *Validate `device` against `/dev/sr*`* — security theatre: the user already has
    shell access and could invoke `cd-paranoia` directly. Adds noise with no real benefit.
  - *Use absolute path for `cd-paranoia`* — fragile; the binary location varies by
    distribution and environment. `shutil.which("cd-paranoia")` would find it but adds
    noise and doesn't address the underlying concern.
  - *Replace with a Python CD-DA library* — the motivation for this commit is precisely
    to eliminate the ctypes `libcdio-paranoia` bindings; subprocess is the intended design.
- **Decision:** `# noqa: S603, S607` is correct. Both calls are trusted internal
  invocations of a known binary with internally-constructed arguments.

---

## LINT-013 — Trusted internal subprocess call for `cdrdao` (`cdrdao_ripper.py`)

- **Rules:** `ruff: S603` (subprocess without `shell=True` check), `ruff: S607` (partial executable path)
- **Locations:**
  - `cdrdao_ripper.py:39–40` — `subprocess.run([...])` in `rip_cdrdao()` (S603 on the `run(` line; S607 on the `[` line)
- **Rationale:**
  - *S603:* The argument list is constructed entirely from hardcoded string literals and the
    caller-supplied `device` string (a device path like `/dev/sr0`). `shell=False` (the default)
    is used; no shell injection surface.
  - *S607:* `"cdrdao"` is intentionally a `PATH` lookup. Hardcoding `/usr/bin/cdrdao` would break
    on systems that install it under `/usr/local/bin` or a non-standard prefix. The binary is a
    well-known disc imaging tool with no impersonation risk in normal environments.
- **Alternatives:**
  - *Validate `device` against `/dev/sr*`* — security theatre: the user already has shell access
    and could invoke `cdrdao` directly. Validation adds noise with no real benefit.
  - *Use `shutil.which("cdrdao")`* — finds the absolute path but adds boilerplate and doesn't
    address the underlying security concern. The `FileNotFoundError` handler in `rip_cdrdao()`
    already provides an actionable error message when cdrdao is not on `PATH`.
- **Decision:** `# noqa: S603, S607` is correct. Same rationale as LINT-012.

---

## LINT-014 — AccurateRip trusted HTTP fetch and array itemsize guard (`accuraterip.py`)

- **Rules:** `ruff: S310` (urllib.request.urlopen with non-literal URL), implicit `S101` rationale (platform guard)
- **Locations:**
  - `accuraterip.py:98` — `urllib.request.urlopen(url, timeout=10)` in `_fetch_ar()`
  - `accuraterip.py:23` — `if array.array("I").itemsize != 4:` platform check
- **Rationale:**
  - *S310:* The `url` passed to `urlopen` is constructed internally in `_ar_url()` with a
    hardcoded `http://www.accuraterip.com/accuraterip` prefix. The only variable components
    are the disc ID hex strings and track count derived from the ripped TOC — no user-supplied
    string reaches the call. The AccurateRip database is HTTP-only; there is no HTTPS
    alternative. The scheme is always `http://` and is not user-configurable.
  - *Platform guard:* The `if array.array("I").itemsize != 4: raise RuntimeError` check verifies
    at module load that unsigned int is 4 bytes (required for u32 LE stereo frame interpretation).
    This is a compile-time platform invariant rather than a runtime guard; it fires on first
    `import cdda2img.accuraterip` on any non-x86 platform where the assumption breaks. Using
    `if`/`raise` rather than `assert` avoids S101 while preserving the fail-fast behaviour.
- **Alternatives:**
  - *Validate URL against an allow-list* — unnecessary; the URL is constructed from internal
    constants. The AccurateRip server is fixed and not configurable.
  - *Use `requests` with cert verification* — AccurateRip is HTTP-only; `requests` would add
    a heavy dependency for no security benefit on a server that does not offer HTTPS.
  - *Use `numpy` for the frame array* — deferred; see verify_rip() docstring.
- **Decision:** `# noqa: S310` on the urlopen call is correct. Platform guard uses if/raise.

---

## LINT-010 — Intentional tuple discard in test fixture unpacking (`test_container.py`)

- **Rule:** `ruff: RUF059` (unpacked variable never used)
- **Locations:**
  - `test_container.py:99` — `rbi, _disc, _` in `test_header_fields_with_rg`
  - `test_container.py:117` — `rbi, _, _` in `test_header_fields_without_rg`
  - `test_container.py:133` — `rbi, _, _` in `test_checksums_pass`
  - `test_container.py:182` — `rbi, _, rg_result` in `test_rg_block_roundtrip`
  - `test_container.py:212` — `rbi, _disc, rg_result` in `test_flac_extraction_rg_tags`
- **Rationale:** The `built_containers` fixture returns `(rbi_path, disc, rg_result)`.
  Different tests need different subsets. `_` and `_disc` signal intentional discard:
  - `_disc` is used where the variable name aids readability (it clarifies what is being
    discarded); `_` is used where the position is self-evident from context.
  - In `test_header_fields_with_rg` and `test_flac_extraction_rg_tags`, the disc
    structure is re-derived independently from the TOC bytes read out of the file — the
    fixture's pre-built `disc` object is intentionally not used, to test the round-trip
    parse rather than assert against the input.
- **Alternatives:**
  - *Index directly: `built_containers["rg"][0]`* — verbose for multi-element access and
    loses the self-documenting tuple unpacking.
  - *Split `built_containers` into sub-fixtures* — would require duplicating the
    expensive transcode + RG analysis setup, or using complex fixture dependencies.
    Disproportionate.
- **Decision:** `_` / `_disc` prefix is correct Python idiom. No code change needed.
  Note: `test_toc_roundtrip` (line 158) uses `disc` without underscore because it IS
  used — `disc.album` and `disc.artist` are asserted against the parsed values.

---

## LINT-015 — S101 assert isinstance guard for sqlite3 connections (`catalogue_menu.py`)

- **Rule:** `ruff: S101` (assert-used)
- **Locations:**
  - `catalogue_menu.py:71` — `_show_summary`
  - `catalogue_menu.py:129` — `_run_search`
  - `catalogue_menu.py:209` — `_show_record`
- **Rationale:** All three functions accept `conn: object` to defer `import sqlite3` to
  call time — the same deferred-import pattern as `metadata_menu.py` (see LINT-005, LINT-006).
  The `assert isinstance(conn, sqlite3.Connection)` guard serves two purposes: it narrows
  the type for subsequent attribute accesses and enforces the caller contract at development
  time. The only caller is `run_catalogue_menu()`, which constructs the connection via
  `open_catalogue_db()` — the assert can never fire in production.
- **Alternatives:**
  - *Accept `sqlite3.Connection` directly* — would move `import sqlite3` to module level,
    defeating the deferred-import pattern. Inconsistent with LINT-005/006.
  - *Restructure to avoid the guard* — `conn: object` typing is intentional to keep the
    module importable without sqlite3 being resolved at parse time.
- **Decision:** `# noqa: S101` is correct. Same rationale as LINT-005/006.

---

## LINT-016 — C901 complexity in interactive TUI dispatch loops (`catalogue_menu.py`)

- **Rule:** `ruff: C901` (complex-structure)
- **Locations:**
  - `catalogue_menu.py:148` — `_results_loop`
  - `catalogue_menu.py:206` — `_show_record`
- **Rationale:** Both functions implement interactive command dispatch loops. `_results_loop`
  handles `n`/`p`/`s`/`q` navigation plus numeric entry selection (with `ValueError` handling
  for non-integer input). `_show_record` handles `n`/`p`/`b` navigation with multi-page track
  display. McCabe complexity is inflated by the exhaustive branch set. Extracting sub-functions
  would require threading `page`, `rows`, `conn`, and return sentinels through the call stack —
  adding indirection without reducing cognitive load. The loop body is linear and easy to follow.
- **Alternatives:**
  - *Extract branch handlers* — e.g. `_handle_nav(choice, page, total_pages)` — adds function
    boundaries that obscure a single conceptual action (respond to one keypress) and still
    requires passing shared state by reference.
  - *Restructure as a state machine class* — disproportionate for a read-only browser.
- **Decision:** `# noqa: C901` is correct. Interactive TUI dispatch loops are a well-established
  pattern where high branch counts are inherent to the design.

---

## LINT-017 — Trusted `sleep` subprocess in a test (`test_track_preview.py`)

- **Rule:** `ruff: S607` (partial executable path)
- **Location:** `test_track_preview.py:36` — `subprocess.Popen(["sleep", "30"])`
- **Rationale:** `test_stop_terminates_playback_and_removes_wav` spawns a real, long-running
  process so it can verify `TrackPreview.stop()` actually terminates playback. `sleep` is
  a coreutils binary invoked by name with two hardcoded literal arguments — no external
  input. S603 does not fire (ruff does not flag a hardcoded `Popen` list); only S607
  (partial path) applies.
- **Alternatives:**
  - *Use a fake process object* — would need a `cast()` to satisfy the type checker and
    would not exercise real OS process termination; the real subprocess is the honest test.
  - *Absolute path to `sleep`* — fragile across distributions; `sleep` is universally on `PATH`.
- **Decision:** `# noqa: S607` is correct — a hardcoded coreutils invocation in a test.
