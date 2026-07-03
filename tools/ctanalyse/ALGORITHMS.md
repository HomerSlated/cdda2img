# ctanalyse — algorithm notes (the porting contract)

Extracted from the CUETools sources (`private/code/cuetools.net/`), 2026-07-02. The C
implementation is written against THIS document, not against the C# directly. Items
marked **PIN-BY-GATE-3** are conventions that must be empirically confirmed against the
real `parity.bin` + `good.pcm` corpus (plan §Phase 3 gate 3) before being trusted.

Source provenance: RS codec by Masayuki Miyazaki (GPL,
sourceforge.jp/projects/reedsolomon/), adapted in CUETools (GPL) by Grigory Chudov.
`Galois.cs` / `RsDecode.cs` are UTF-16LE with Japanese comments; transcode with
`iconv -f UTF-16LE -t UTF-8` before reading.

## 1. Units and grid layout (`CDRepair.cs` ctor, `Write`)

- The atom is the 16-bit **word** (one mono s16le sample); one GF(2¹⁶) symbol per word.
  A stereo sample pair = 2 words. All "stride" arithmetic is in words.
- `stride` = **internal** stride = 2 × the wire `stride` attribute from the lookup XML
  (`DBEntry.cs: this.stride = ctdbRespEntry.stride * 2`). Tracy Chapman entry 67116:
  wire 5880 → internal **11760 words** (= 10 sectors).
- Total words `W = finalSampleCount * 2` (finalSampleCount = stereo samples,
  162892 × 588 = 95,780,496 stereo samples → W = 191,560,992 words for our disc).
- `stridecount = W / stride − 2` (rows of the codeword grid),
  `laststride = stride + (W % stride)`.
- Word `w` maps to **column** `part = w % stride` and **row** `r = w / stride`.
  - Row 0 (first `stride` words) is **leadin** — excluded from the codeword.
  - Rows 1..stridecount (inclusive) are the codeword data: column `part`'s data symbols
    are `d_j = word[(j+1)*stride + part]`, j = 0..stridecount−1 (oldest first).
  - The tail (`laststride = stride + W%stride` words) is **leadout** — excluded.
  - The exclusion of one full stride at each end is what makes offset tolerance work:
    a shift of |offset| < stride/2 stereo samples only moves boundary words between
    leadin/leadout and the first/last rows, which is correctable in syndrome space.
  - C# keeps `leadin[2*stride]` (rows 0–1) and `leadout[stride+laststride]` (row
    stridecount + tail; **reversed indexing**: `leadout[remaining]` where remaining
    counts back from disc end). ctanalyse holds the whole PCM in memory (383 MB), so it
    reads boundary words directly instead of maintaining these buffers.
- Constraint: `(W + stride − 1)/stride + npar ≤ 65535` (codeword length must fit GF).

## 2. The field (`Galois.cs`)

GF(2¹⁶), polynomial **0x1100B**, generator α = 2, `symStart = 0`.

- `expTbl[0..2*65535)`: doubled so `mul` needs no mod: `expTbl[i] = expTbl[65535+i]`.
  Built by `d <<= 1; if (d >> 16) & 1: d = (d ^ 0x1100B) & 0xFFFF` from d = 1.
- `logTbl[0..65535]`, `logTbl[expTbl[i]] = i`; log(0) undefined (callers guard).
- `mul(a,b) = (a && b) ? exp[log a + log b] : 0`; `mulExp(a,k) = a ? exp[log a + k] : 0`
  (k already a log; relies on doubled exp table); `divExp(a,k) = a ? exp[log a − k +
  65535] : 0`; `inv(a) = exp[65535 − log a]`; `toPos(n, a) = n − 1 − log(a)` maps a
  Chien root to a codeword position.
- Generator polynomial `G(x) = Π_{k=0}^{npar−1} (x + α^k)` (`makeEncodeGx`; stored
  high-degree-first).
- **Decode split tables** (`makeDecodeTable`) — the scalar kernel we port:
  `T[b][0][i] = mul(b, α^i)` and `T[b][1][i] = mul(b<<8, α^i)` for b = 0..255, so
  `mul(x, α^i) = T[x & 0xFF][0][i] ^ T[x >> 8][1][i]` — two 256-entry u16 tables per
  constant, 1 KB per i, 16 KB total for npar 16, L1-resident.

## 3. Syndromes

Definition per column (data-only; Horner form, oldest symbol first):

```
S_i = Σ_{j=0}^{stridecount−1} d_j · α^(i·(stridecount−1−j))      i = 0..npar−1
i.e.  S_i ← d_j ⊕ S_i·α^i   for each successive word of the column
```

Equivalent direct form used by `ProcessStride16`: word at row r contributes
`d · α^(i·n)` with `n = stridecount − r` (n = distance from the last data row).

**Production subtlety (`AccurateRip.cs: CalculateCRCs` + `GetSyndrome`):** CUETools does
NOT accumulate syndromes in its streaming pass — it accumulates the systematic-encoder
**parity** (LFSR against G(x), `SyndromeCalc16` despite the name), then converts
parity→syndrome afterwards via `ParityToSyndrome.Parity2Syndrome`:

```
S_x(column) = Σ_{j=0}^{npar−1} par_j · α^(−(1+j)·x)        (exponent taken mod 65535)
```

with a column rotation `y1 = (y − offset + stride2) % stride2` when an offset is baked
in. ctanalyse never encodes, so we skip the LFSR entirely and accumulate data syndromes
directly with the §2 decode tables. **PIN-BY-GATE-3:** the exact exponent convention of
the *wire* syndromes (the `−(1+j)·x` form, and the "Additional +ii because we use
slightly different syndrome" comment in `Syndrome2Parity`) — confirmed when the clean
disc + real parity.bin cancel to zero.

## 4. Wire formats

### Lookup XML (`db.cuetools.net/lookup2.php`, cached as `ctdb.xml`)

Per `<entry>`: `id`, `confidence`, `npar`, `stride` (wire; ×2 for internal), `crc`
(hex CRC32 of the codeword region, consensus alignment — see §7), `trackcrcs` (hex,
space-separated, per-track full-track CRC32 — validated in stage 1), `hasparity` (URL,
e.g. `http://p.cuetools.net/67116`), `syndrome` (base64, npar syndromes of **one**
column — `Bytes2Syndrome(1, npar, …)` in `DBEntry.cs` — the quick-reject sample),
`toc`, plus metadata.

### Parity file (`p.cuetools.net/<id>`, cached as `parity.bin`)

Despite the name, the file contains **syndromes, not parity symbols**
(`CUEToolsDB.cs: FetchDB` feeds it to `Bytes2Syndrome`; the submit path uploads
`Syndrome2Bytes(GetSyndrome(...))`).

- Fetch: plain GET with a **Range** header for the first `npar × stride_internal × 2`
  bytes (16 × 11760 × 2 = 376,320 for our disc). The server file may be longer
  (larger-npar submissions); the prefix is valid because syndromes are stored
  syndrome-major.
- Layout (`Syndrome2Bytes`/`Bytes2Syndrome`): u16 little-endian,
  **syndrome-major**: `file_u16[i * stride + j]` = S_i of column j. In-memory C# form
  is the transpose `syndrome[column j, syndrome i]`.

### What verification means

Both our disc and the DB reference produce data-only syndromes over the same grid. By
RS linearity, the **error syndrome** per column is simply:

```
E_i(part) = S_i^{ours}(part) ⊕ S_i^{DB}(part')     (part' = offset-rotated column)
```

Zero → column clean. Non-zero → run BM/Chien/Forney on E to locate/correct. (The C#
`CDRepairEncode.VerifyParity` reaches the same E by appending the reference *parity*
symbols to our data syndrome — the older representation; the syndrome-file path is the
current one and the simpler port.)

## 5. Offset handling

- Offset unit: **stereo samples** (1 offset = 2 words). Positive = our data is late
  relative to the reference (reference reads our word w as w + 2·offset).
- Column mapping: our column `part` corresponds to DB column
  `part2 = (part + 2·offset + stride) % stride`.
- Boundary correction: for the ≤ |2·offset| columns whose first/last codeword word
  falls off the row-1..stridecount window, adjust the syndrome by removing the word
  that left and adding the word that entered (multiplied by α^(i·(stridecount−1)) for
  the oldest position), sourced from leadin/leadout — in ctanalyse, directly from the
  PCM. C# reference: `CDRepair.cs: FindOffset`/`VerifyParity` (uses
  `i·(stridecount−1)`) vs `AccurateRip.cs: GetSyndrome` (uses `i·stridecount`) — the
  two differ because one adjusts data syndromes and the other parity-derived syndromes.
  **PIN-BY-GATE-3.**
- Legal range: |offset| < stride_internal/2 stereo samples… strictly
  `1 − stride/2 ≤ offset·2 < stride/2` in words (C# loops offset over
  `[1 − stride/2, stride/2)` — ±2939 stereo samples for internal stride 11760…
  **PIN-BY-GATE-3**: verify the loop variable's unit against the found −669).
  Our known case (−669) is comfortably inside either reading.

### FindOffset (cheap, robust)

`CDRepair.cs: FindOffset` sweeps candidate offsets using **one column only**
(`part2 = 0`): compute that column's error syndrome at each candidate offset, run BM,
and accept the first offset where BM's degree equals `allowed_errors` and Chien finds
that many roots — escalating `allowed_errors` from 0 to npar/2 − 1 in an outer loop, so
a clean-at-some-offset disc exits on the first pass and a damaged one still resolves.
Cost per candidate: npar Horner steps + a tiny BM — the sweep is microseconds-scale;
the expensive part is only the one-time whole-disc syndrome pass.

## 6. Decode per column (`RsDecode.cs`, Miyazaki)

Codeword length for position arithmetic: `n = stridecount + npar`.

1. **Modified Berlekamp–Massey** `calcSigmaMBM(sigma, syn)` → error-locator σ(z),
   returns degree `jisu` (≤ npar/2) or −1. Straight port; works on npar ints.
2. **Chien search** `chienSearch(pos, n, jisu, sigma)`: find jisu roots of σ within
   data length; optimisation: σ1 = sum of all roots, so the last root is derived by
   subtraction. Has a GF(2¹⁶) fast path (`chienFast`, batched increments); port the
   plain path first, fast path only if profiling says so.
3. **Forney** `doForney(jisu, root, sigma, omega) = root · ω(z)/σ′(z)` with
   ω = σ·S mod z^(npar/2+1) (`mulPoly(o, s, syn, npar/2+1, npar, npar)`); the result is
   the **XOR mask** for the erroneous word.
4. Position → word offset (`CDRepairFix.GetErrOff`):

   ```
   erroff = (2 + toPos(stridecount + npar, errpos) −
             (stride + part + 2·actualOffset) / stride) · stride + part
   ```

   yielding a word offset into OUR PCM (the C# then writes `data[erroff] ^= forney`
   during its streaming re-write, filtering `0 ≤ erroff < W`). ctanalyse emits
   `{byte: 2·erroff, old: word, new: word ^ mask}` triples — already in our domain.
5. `can_recover = false` when any non-zero column has BM fail (degree ≤ 0) or Chien not
   find `jisu` roots. All-or-nothing per disc.
6. **Erasure extension (item 8, IMPLEMENTED 2026-07-03):** `rs_decode_column()` takes an
   optional erasure-position list per column; with e erasures and t errors correction holds
   when e + 2t ≤ npar (each erasure worth half an error). Path: erasure locator
   Γ(x)=∏(1−X_i·x) with X_i = α^(n_data−1−p_i); modified syndromes T = Γ·E mod x^npar; BM on
   the clean tail T[e..npar−1] for the t unknown errors; combined errata locator Λ = σ·Γ;
   Chien + Forney over Λ; then a syndrome **re-validation** (recompute S from the found
   errata, require == E) that refuses over-capacity miscorrection before it reaches the
   CRC/AR gate. `--erasures <bitmap>` (one LSB-first bit per local 16-bit word) feeds it;
   `cta_build_erasures` maps each flagged word to (column, row) by inverting the syndrome
   transform: u = w − 2·offset − stride, part2 = u mod stride, row = u / stride — so
   erasures land in exactly the grid cells the syndromes describe. Per column: try
   errors-and-erasures, fall back to error-only if it fails (false-positive C2 flags cost a
   slot but never corrupt). The C2 experiment (tools/c2read, tools/c2bench.py) confirmed C2
   is precise enough to make good erasures; validated end to end on real 40× damage
   (`ctdb_repair.py --c2`, AR conf 200).

## 7. CRC model

- `entry.crc` (lookup XML) = CRC32 of the **codeword region only** (leadin/leadout
  excluded), in **consensus alignment**: `CTDBCRC(0, offset, stride/2, laststride/2)` —
  i.e. the disc minus the first `stride/2` and last `laststride/2` stereo samples,
  window shifted by the detected offset. C# reference: `AccurateRip.cs: CTDBCRC`,
  `CDRepairEncode.OffsettedCRC`.
- `trackcrcs` = plain CRC32 (`zlib.crc32`) per track, but the windows are **edge-aware**
  (CONFIRMED empirically 2026-07-02 against entry 67116 at offset −669):
  - interior tracks: full `[INDEX01, next INDEX01)` window (this is all stage 1 tested);
  - track 1: window starts `stride/2` stereo samples into the disc;
  - last track: window ends `laststride/2` stereo samples before the leadout.
  The disc-edge regions are excluded because their content depends on each submitter's
  drive offset (same reason the codeword grid excludes leadin/leadout). C# reference:
  `AccurateRip.cs: CTDBCRC(iTrack, oi, stride/2, laststride/2)` with
  `prefixSamples += oi; suffixSamples -= oi`. Implemented in
  `tools/ctdb_repair.py: track_crc_at`.
- ctanalyse reports `crc_before`/`crc_after` as the §7 region CRC at the found offset;
  Python's gates additionally re-check per-track `trackcrcs` in our domain and re-run
  our AccurateRip on the spliced result. The DB region-CRC is the *consensus* check;
  the AR check is the *our-archive* check; both must pass.

## 8. C port shape

- Input: `--pcm` (raw s16le), `--parity` (the syndrome file), `--npar`, `--stride`
  (wire value; ×2 internally), `--toc` (INDEX-01 LBAs + leadout, for affected-track
  reporting), `--impl`, `--threads`.
- Pass 1 (the only expensive step): stream the PCM once, accumulate
  `S[stride][npar]` data syndromes with the §2 split tables; columns are independent →
  partition columns across threads. ~3.1 G table-lookup MACs for our disc.
- Pass 2: FindOffset (single column sweep). Pass 3: per-column E = S_ours ⊕ S_DB with
  offset rotation + boundary fixes; BM/Chien/Forney per dirty column; collect
  corrections. Pass 4: apply corrections to an in-memory copy, compute `crc_after`,
  emit JSON. Peak RSS ≈ PCM + tables + syndromes ≈ 400 MB.
- All multi-byte reads are explicit little-endian; no struct punning of the wire file.

## 9. Test vectors (`CUETools/CUETools.TestParity/`)

- `CDRepairDecodeTest.cs`: synthetic image `TestImageGenerator("0 9801", seed 2423,
  32*588, 0)`, stride 10·588·2 = 11760, offset ±48; expected encode CRC **377539636**;
  base64 syndromes (`encodeSyndrome`, ±offset variants) and parity (`encodeParity[8]`,
  `[16]`) — port the generator's PRNG to reproduce, or capture the byte streams once.
- Small hard vectors: `g16.gfconv([1,2,3],[4,3,2]) = [5, 33657, 33184, 33657, 5]`,
  `gfconv([1,2,3],[4,-1,2]) = [5, 6, 1774, 4, 5]` (log-domain, −1 = zero);
  `RsEncode8`/`RsDecode8` syndrome vector `{219, 96, 208, 202, 116, 211, 182, 129}`.
- Our live vectors (stronger than all of the above): `private/testdata/ctanalyse/`
  corpus — clean disc must cancel against `parity.bin` at offset −669 with zero
  corrections (gate 3); `splice8.pcm` must repair to track-8 CRC `c9719806` (gate 4);
  `bad40x.pcm` exercises recover-or-honest-refusal (gate 5).
