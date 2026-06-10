# Rip Engine Benchmark — cdrdao vs cd-paranoia

*Research notes for the cdda2img project.*

Wall-clock comparison of the two rip back-ends (`cdrdao read-cd` primary,
`cd-paranoia` fallback) and their paranoia levels, on a **clean** commercial
audio CD. Captured 2026-06-10.

---

## 1. Setup

- **Drive:** Plextor PX-716A (the development drive; read offset **+30**), `/dev/sr0`.
- **Disc:** a single clean, full-length commercial audio CD (no visible damage).
- **Method:** `time <cmd>`, single run each. Wall-clock only; run-to-run noise is
  ~1–2% (spin-up, seek, thermal, OS cache), so differences below ~3 s are not
  significant.
- **Caveat:** one disc, one drive, single runs. These numbers characterise *this
  drive on clean media*; a damaged disc would change the paranoia picture entirely
  (see §5).

Commands:

```bash
# cdrdao primary (generic-mmc driver)
time cdrdao read-cd --device /dev/sr0 --driver generic-mmc --paranoia-mode 0 --datafile test.bin test.toc
time cdrdao read-cd --device /dev/sr0 --driver generic-mmc --paranoia-mode 3 --datafile test.bin test.toc

# cd-paranoia fallback (libcdio-paranoia 10.2), offset +30 applied at read time
time cd-paranoia -r -O 30 -Z 1- /tmp/output.raw   # -Z: paranoia off
time cd-paranoia -r -O 30 -Y 1- /tmp/output.raw   # -Y: cdda2wav-style overlap only
time cd-paranoia -r -O 30    1- /tmp/output.raw   # (no flag): full paranoia
```

## 2. Results

| Engine | Mode | Time | Captures |
|--------|------|-----:|----------|
| cd-paranoia | `-Z` (off) | **108.94 s** | audio only |
| cdrdao | `--paranoia-mode 3` (full) | 138.29 s | audio + **subchannel** + TOC |
| cdrdao | `--paranoia-mode 0` (off) | 140.18 s | audio + subchannel + TOC |
| cd-paranoia | `-Y` (overlap only) | 244.54 s | audio only |
| cd-paranoia | full (no flag) | 246.91 s | audio only |

## 3. Within-engine: the paranoia *level* is ~free on a clean disc

Both engines show the same pattern — the paranoia level barely moves the clock:

- cdrdao: mode 0 (140.18 s) ≈ mode 3 (138.29 s) — Δ within noise.
- cd-paranoia: `-Y` (244.54 s) ≈ full (246.91 s) — Δ within noise.

Full paranoia's expensive behaviour (re-reading a sector until reads agree, plus
scratch detect + repair) only triggers on **errors**. On a pristine disc the
comparisons agree on the first pass, so "full" does essentially the same number of
reads as the lighter level. The extra-paranoia layer is therefore *free on clean
media* — it costs time only when it finds something to fix.

## 4. Cross-engine: the gap is overlap re-reading, not thoroughness

The 1.77× gap between the engines at their "default" levels is **not** because
cd-paranoia is more careful. Two measurements isolate the cause:

- **Overlap is the cost.** cd-paranoia full (246.91 s) − `-Z` (108.94 s) = **138 s
  of pure overlap re-reading.** cdda2wav-style overlap (present at every level
  except `-Z`) re-reads each chunk's boundary sectors to defeat drive jitter,
  reading far more total sectors than a linear pass. It *more than doubles* the
  read on this drive. Since §3 showed the smart layer is free, this overlap
  baseline is the entire engine-to-engine difference.

- **Bare cd-paranoia (109 s) is faster than cdrdao (138 s).** `-Z` disables the
  overlap and collapses to a straight linear audio read — the theoretical floor —
  and beats cdrdao mode-0 by ~30 s (~22%). So cdrdao mode-0 is **not** a bare
  audio read: it captures subchannel (MCN scan via `readCatalogScan`, per-track
  ISRC, pregap/index) and analyses the TOC even with paranoia off. Reading audio
  *with* subchannel typically forces a slower drive mode, and the analysis adds
  passes. That ~30 s is the price of the metadata cd-paranoia throws away.

  *Inference, not measurement:* wall-clock can't separate "subchannel slows the
  read" from "extra analysis pass" from "read-speed negotiation". The cdrdao
  `-v 4` log timestamps the audio read vs the catalog/ISRC scan and would attribute
  the 30 s precisely. (Not yet captured.)

## 5. What you pay for — and why the architecture is right

Ranked by cost against the raw floor:

| Time | What it buys |
|-----:|--------------|
| 109 s | bare audio (cd-paranoia `-Z`) — no metadata, no jitter defense |
| 138 s | audio + **full subchannel metadata** + cdrdao paranoia (free here) |
| 247 s | audio + overlap jitter defense, **no subchannel metadata** |

Conclusions:

- **cdrdao-as-primary is empirically correct.** On a clean disc cdrdao mode 3
  (138 s) **dominates cd-paranoia full (247 s) on both axes** — faster *and* it
  captures MCN/ISRC/CD-Text. There is no clean-disc scenario where cd-paranoia
  full beats the primary. The ~30 s cdrdao "costs" over the raw floor *is* the
  subchannel capture that justifies it being primary.
- **The cd-paranoia fallback's real niche is the fast `-Z` audio-only pass**
  (109 s), not its full-paranoia mode (slower *and* metadata-blind). Its value as
  a fallback remains toolchain independence (survives cdrdao *run* failures on a
  readable disc), not superior recovery — same paranoia-algorithm ceiling.
- **Validates the planned two-pass refinement** (CLAUDE.md, rip-strategy section):
  a fast `-Z` pass → AccurateRip check → full paranoia only on failure would give
  the *fallback* path a 2.25× speedup on clean media (109 vs 247 s) at zero quality
  cost, since AccurateRip catches any error the skipped overlap would have. It
  helps **only** the fallback — for the cdrdao primary, mode 0 ≈ mode 3, so there
  is no speed to win there.
- **Clean media only.** All of this inverts on a damaged disc, where full paranoia
  *earns* its re-reads and the overlap that looks wasteful here becomes the point.

## 6. Cross-references

- `CLAUDE.md` — "Rip Strategy (`rip` subcommand — `_rip_with_fallback`)" and the
  cd-paranoia paranoia-level table (`-Z`/`-Y`/full ↔ `disc_reader.py:_PARANOIA_FLAGS`).
- cdrdao internals (read from `private/code/cdrdao/`): `CdrDriver::paranoiaMode`
  (`dao/CdrDriver.cc:4204`, modes 0..3), `readCatalogScan` (MCN, CRC + median vote,
  `dao/CdrDriver.cc:3867`), `GenericMMC::readIsrc` (`dao/GenericMMC.cc:1620`).
  Verbose: `cdrdao ... -v 4` (level threshold; default 2).
