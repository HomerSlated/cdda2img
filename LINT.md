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

## LINT-003 — PyAV `add_stream(template=)`: missing stub overload

- **Rule:** `ty: no-matching-overload` / `call-overload`
- **Location:** `replaygain.py:244` — `out_c.add_stream(template=in_stream)`
- **Rationale:** `add_stream(template=<AudioStream>)` is the documented PyAV API for
  stream-copy remux (re-muxing packets without re-encoding). The `template=` keyword
  parameter is not present in any of the three typed overloads in `av/container/output.pyi`,
  making this a stub gap, not a code error. The PyAV documentation and source explicitly
  support this call pattern.
- **Alternatives:**
  - *`out_c.add_stream(codec_name=in_stream.codec_context.name, rate=...)`* — **rejected**.
    Using `codec_name=` would trigger re-encoding rather than stream copy, which is the
    wrong behaviour (we want bitwise-identical audio packets, just with updated metadata).
  - *Contribute the `template=` overload to PyAV stubs* — correct long-term fix.
- **Decision:** `# type: ignore[call-overload]` is correct.

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

## LINT-008 — Trusted internal subprocess call for `ffplay` (`audition.py`)

- **Rule:** `ruff: S603` (subprocess without `shell=True` check)
- **Location:** `audition.py:154` — `subprocess.Popen(cmd, stdin=subprocess.DEVNULL)`
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
