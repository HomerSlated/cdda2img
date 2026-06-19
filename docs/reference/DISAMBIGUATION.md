# Disc Identification & Disambiguation — Manual Interrogation Log

A controlled, step-by-step interrogation of the identification and disambiguation
problem, driven by the user one question at a time. Each Q/A pair is logged verbatim
in §3. **No conclusions are drawn until the user signals completion.**

## 1 — Premise

- **Ignore all baked-in disc metadata** (MCN, CD-Text, subchannel ISRC). It is almost
  never present on real discs, and the worked example (U2 *Joshua Tree*) confirmed the
  common case: `MCN = 0000000000000` (null), no usable subchannel identity.
- **Start from zero prior knowledge.** The only inputs are the physical disc's **TOC**
  (track layout) and its **raw PCM** (the audio). Everything else must be computed from
  those two, or fetched from a remote service keyed by something computed from those two.
- This mirrors exactly what the live pipeline faces on a no-metadata disc.

## 2 — Identification Methods

### 2a — Locally computable (no network, no baked metadata)

Every remote query is keyed by one of these. They are pure functions of TOC and/or PCM.

| Artifact | Derived from | Algorithm / code | Output | Keys into |
|----------|--------------|------------------|--------|-----------|
| **TOC (raw)** | drive read | cdrdao `read-cd` / cd-paranoia `-Q` | track count, per-track start LSN, lead-out LSN | basis of every disc ID below |
| **Per-track runtime** | TOC | LSN deltas ÷ 75 | seconds/track | duration matching, sanity |
| **Per-disc runtime** | TOC | lead-out ÷ 75 | total seconds | duration matching, sanity |
| **MusicBrainz Disc ID** | TOC | `mb_lookup.compute_disc_id` — SHA-1 of an 804-char uppercase-hex TOC string, base64 with `+/=`→`._-` | 28-char string | MB disc-ID lookup |
| **CDDB / freedb Disc ID** | TOC | `cddb.compute_cddb_disc_id(track_lsns, disc_last_lsn)` | 8 hex chars | CDDB query; AR URL component |
| **AccurateRip disc IDs** | TOC (LSNs) | `accuraterip._ar_disc_ids` — `id1 = Σlsn + leadout`; `id2` = LSN-weighted sum | `(id1, id2)` uint32 | AR URL |
| **AccurateRip track CRCs** | **PCM** | `accuraterip._ar_checksums` — multiplier-weighted u32 sum, ±2940-frame boundary exclusion | v1 + v2 per track | AR match (correctness + presence) |
| **Chromaprint fingerprint** | **PCM** | `fpcalc` via `acoustid_lookup.fingerprint_and_lookup` | per-track acoustic fingerprint | AcoustID query |

Note the split: the four **disc IDs** are positional (TOC-only) — they collide for any two
pressings sharing a track layout. The two **PCM signatures** (AR CRC, Chromaprint) fingerprint
the audio itself, so they can in principle distinguish different masters of the same layout.

### 2b — Remote queries

| Service | Query key (from §2a) | Code entry point | Returns | Native granularity |
|---------|---------------------|------------------|---------|--------------------|
| **MusicBrainz** (disc-ID) | MB Disc ID | `mb_lookup.lookup_disc_id` | releases whose TOC exactly matches; each carries `barcode_hints` | release (exact TOC) |
| **MusicBrainz** (text) | album + artist | `mb_lookup.search_releases` | candidate releases | release |
| **MusicBrainz** (barcode) | a barcode | `mb_lookup.search_releases_by_barcode` | releases with that barcode | release |
| **MusicBrainz** (ISRC) | an ISRC | `mb_lookup.lookup_isrc` | releases containing that recording | recording→release |
| **MusicBrainz** (duration) | per-track + total runtime | `mb_lookup.duration_match_lookup` | best release whose durations match (±) | release |
| **MusicBrainz** (rel-group) | release-group MBID | `mb_lookup.lookup_release_group` | sibling releases (for "original" lookup) | release-group |
| **Discogs** (barcode) | a barcode | `discogs_lookup.search_by_barcode` | releases with that barcode | release / pressing |
| **Discogs** (text) | artist + title | `discogs_lookup.search_releases` | candidate releases | release / pressing |
| **CDDB** | CDDB Disc ID | `cddb.query_cddb` | flat `Artist / Title` + track titles | disc (lowest trust) |
| **AccurateRip** | AR id1/id2 + CDDB id | `accuraterip.verify_rip` | per-track CRC blocks + confidence counts | master / era (CRC-grouped) |
| **AcoustID** | Chromaprint fp | `acoustid_lookup.fingerprint_and_lookup` | recording MBIDs → releases | recording (per track) |

### 2c — Excluded under the premise (baked-in, not computable from TOC+PCM)

| Identifier | Where it lives | Why excluded |
|------------|----------------|--------------|
| **MCN / UPC-EAN** | Q-channel Mode 2 | Subchannel metadata; almost never present (null on the worked-example disc) |
| **CD-Text** (title/performer/…) | R-W subchannels, lead-in | Subchannel metadata; rarely authored |
| **ISRC** | Q-channel Mode 3 | Subchannel metadata — **cannot be derived from PCM**; not present on most discs |

> Correction noted for the record: ISRC is *not* computable from the audio. It is a
> subchannel-read value, so it falls under "baked-in metadata" and is excluded here.
> The locally-derivable IDs are the disc IDs and the PCM checksums/fingerprints only.

## 3 — Interrogation Log

> Format: each entry is the user's question, then the computed/queried answer presented
> at the time. No interpretation added beyond what the data states.

### Q1 — Compute the MusicBrainz Disc ID for the disc in the drive

**Method:** local computation, no network. `cdrdao read-toc` (TOC-only scan, no audio rip)
on `/dev/sr0` → `mb_lookup.disc_id_from_rbi` via `tools/disc_id.py`.

**Result:**

```
MusicBrainz Disc ID:  6E_gWEkDHQ0ROLKhj.vyupe298I-
tracks:               11
```

TOC layout that produced it (the `INDEX 01` LBA column is what enters the SHA-1 hash):

| # | start frame | pregap | INDEX 01 LBA |
|---|-------------|--------|--------------|
| 1 | 0 | 32 | 182 |
| 2 | 25380 | 0 | 25530 |
| 3 | 46192 | 48 | 46390 |
| 4 | 68452 | 0 | 68602 |
| 5 | 88862 | 0 | 89012 |
| 6 | 108182 | 0 | 108332 |
| 7 | 130167 | 60 | 130377 |
| 8 | 143505 | 0 | 143655 |
| 9 | 159430 | 0 | 159580 |
| 10 | 183655 | 0 | 183805 |
| 11 | 202655 | 0 | 202805 |
| lead-out | — | — | 226197 |

Hash inputs: `first_track=01`, `last_track=11`, `lead_out_LBA=226197`, then the 11 INDEX-01
LBAs above (padded to 99 zero offsets).

**Observations (no conclusion):**
- The TOC scan reported 28–149 Q-subchannel CRC errors per track and "Found disk catalogue
  number" — the latter is a known false positive on this disc (value is null; confirmed by an
  earlier `disc_scan --deep`). Neither affects the disc-ID, which is TOC geometry only.
- Pregaps present on tracks 1 (32f), 3 (48f), 7 (60f); all other tracks have none.

### Q2 — Assuming MusicBrainz does not exist and we lack the disc-ID, what can we compute locally to query the *other* services?

**Method:** local computation from the same TOC (`/var/tmp/disc_q.toc`), no network.
`track_lsns` / `disc_last_lsn` derived exactly as `cdrdao_ripper` does
(`start_frame + pregap_frames`; last = `…+ duration_frames − 1`), then fed to the project's
own `cddb.compute_cddb_disc_id`, `accuraterip._ar_disc_ids`, `accuraterip._ar_url`.

```
track_lsns    : [32, 25380, 46240, 68452, 88862, 108182, 130227, 143505, 159430, 183655, 202655]
disc_last_lsn : 226046   (lead-out LSN = 226047)
```

**Computable query keys, by service:**

| Service | Query key | Value (this disc) | Available now? |
|---------|-----------|-------------------|----------------|
| **CDDB** | CDDB disc ID (TOC) | `8a0bc50b` (int 2316027147) | ✅ TOC-only |
| **AccurateRip** | id1 / id2 / cddb-id (TOC) | `0015190b` / `00b48124` / `8a0bc50b` | ✅ TOC-only (presence + stored CRCs; CRC *match* needs PCM) |
| **AcoustID** | Chromaprint fingerprint (PCM) | — | ⛔ requires a rip (no PCM yet) |
| **Discogs** | barcode / cat-no / artist+title | — | ⛔ no disc-computable key exists |

AccurateRip request URL:
`https://www.accuraterip.com/accuraterip/b/0/9/dBAR-011-0015190b-00b48124-8a0bc50b.bin`

**Per-track & total runtime** (computable, but *not* a query key for any remaining
service — CDDB/Discogs accept no duration query; only the excluded MB did):

```
 #   frames     M:S.F     secs
 1    25348  05:37.73   337.97
 2    20812  04:37.37   277.49
 3    22212  04:56.12   296.16
 4    20410  04:32.10   272.13
 5    19320  04:17.45   257.60
 6    21985  04:53.10   293.13
 7    13278  02:57.03   177.04
 8    15925  03:32.25   212.33
 9    24225  05:23.00   323.00
10    19000  04:13.25   253.33
11    23392  05:11.67   311.89
TOT  225907  50:12.07  3012.09
```

**Observations (no conclusion):**
- Two services are directly reachable from local data alone (CDDB, AccurateRip), both keyed
  off the TOC; the CDDB disc ID `8a0bc50b` is reused inside the AccurateRip request.
- AcoustID becomes reachable only after a rip (it needs PCM to fingerprint).
- Discogs has *no* query key derivable from a disc — it is reachable only once another
  service supplies a barcode, catalogue number, or artist/title to feed it.

### Q3 — Explain AccurateRip in detail: query types, result fields, ID granularity, full scope

**Method:** code review (`accuraterip.py`) + live fetch of this disc's dBAR over HTTPS
(`_fetch_ar` + `_parse_dbar`, keys from Q2). 2464-byte body, transport=https, 22 blocks.

**Nature of the service.** A *rip-correctness* database, **not** a metadata database. It
answers only "do my per-track audio checksums match what other drives produced for this
disc?" It stores **no** artist / title / barcode / catalogue / track names. (The commercial
*AccurateRip Meta* metadata DB inside dBpoweramp is a different, proprietary product keyed by
the same fingerprint — not what we query.)

**Query model — one query type, per-disc only.**

| Property | Value |
|----------|-------|
| Query granularity | **Per disc** — no per-track query exists |
| Key | `(id1, id2, cddb_id)` + track count, all TOC-derived |
| Transport | HTTPS preferred, HTTP fallback |
| URL | `…/accuraterip/b/0/9/dBAR-011-0015190b-00b48124-8a0bc50b.bin` |
| Path quirk | `/b/0/9/` = last 3 hex chars of `id1` **reversed** (LSB-first) |

→ **IDs are per-disc, not per-track.** The fingerprint that *addresses* the DB is disc-level;
per-track CRCs come back as *results*, not as addressable keys. (A submission POST protocol
also exists for contributing rips — we only consume.)

**Response wire format** — flat sequence of *blocks*, each an independent agreement group:

```
Block header   13 bytes   <BLLL  = n_tracks, id1, id2, cddb_id
Per track ×N    9 bytes   <BLL   = conf (u8), crc (u32), crc450 (u32)
```

| Field | Meaning |
|-------|---------|
| `conf` | count of submitters who produced this exact CRC for this track |
| `crc` | a **single** AR checksum — v1 *or* v2 by submitter era; we compute both locally and test each against this slot |
| `crc450` | CRC of **frame 450 only**, for blind offset detection — **not** the v2 checksum; zero in older v1 blocks |

**Why blocks ≠ editions:** `crc` is offset-sensitive and the DB mixes corrected/uncorrected
submissions, so offset variation inflates block count past the real pressing count. AR
distinguishes **masters** (different audio → different CRC) but cannot enumerate editions and
cannot split byte-identical country variants of one master.

**This disc's live response (22 blocks), per-track conf near-flat within each block:**

```
block  0: conf 200  (track1 crc=73df9aee)   block 11: conf 10
block  1: conf 200  (track1 crc=f1670c8d)   block 12: conf  6
block  2: conf 200  (track1 crc=9d949131)   block 13: conf  5
block  3: conf ~150                         block 14: conf  4
block  4: conf ~131                         block 15-19: conf  2
block  5: conf ~114                         block 20-21: conf 0/2 (crc=0 placeholder slots)
block  6: conf ~110
block  7: conf ~72     crc450 populated through block 10;
block  8: conf ~46     zero from block 11 down (older v1-era submissions)
block  9: conf ~45
block 10: conf ~27
```

Three distinct conf-200 CRC groups at the top (flat 200 looks like a display ceiling).

**Parsed result fields we ultimately expose:**
- Per track (`ARTrackResult`): `track`, computed `v1_crc`, computed `v2_crc`,
  `confidence_v1`, `confidence_v2`, `max_confidence` (highest single-block conf),
  `total_confidence` (sum across blocks). `None` = no match / not in DB.
- Per disc (`ARVerifyResult`): `tracks[]`, `transport`, `dbar_b3sum` (BLAKE3 of raw bytes).

**Observations (no conclusion):**
- AccurateRip confirms rip correctness and places our bytes in a master-level CRC group; it
  returns no descriptive metadata and cannot by itself name a pressing.
- The CRC *match* step needs PCM (a rip); the dBAR *fetch* shown here is TOC-only.
- Boundary rule: first/last 5 frames (2940) of the disc excluded from each track's CRC.

### Q4 — Rip the PCM, compute the AcoustID Chromaprint fingerprints, and explain the full query/response scope

**Method:** `cdrdao read-cd` full rip → `/var/tmp/jt.pcm` (s16le, **531,662,544 bytes =
226047 × 2352**, i.e. full disc from LSN 0). Per-track program audio sliced at
`audio_start_frame × 2352` for `duration_frames × 2352`, fingerprinted **locally** with
libchromaprint (no AcoustID query). Code review of `acoustid_lookup.py` for the query path.

**Environment note:** the `fpcalc` binary fails on this box for *every* input
("Could not create an audio converter instance" — FFmpeg 6.x / Chromaprint 1.5.1
SwResample bug), even native 44.1k/16/stereo. Worked around by feeding decoded PCM straight
into `acoustid.fingerprint(44100, 2, pcmiter)` (libchromaprint via ctypes). **Latent bug:**
the live pipeline's `acoustid.match` prefers `fpcalc`, so the rip pipeline's AcoustID step
would currently fail at fingerprinting on this machine.

**Granularity:** **per-track / per-recording** — the exact inverse of AccurateRip's per-disc
model. Each track is fingerprinted and queried independently; no disc-level AcoustID query.

**The query** (`/v2/lookup`, one per track):
- `client` — application API key
- `duration` — **full** track length in seconds
- `fingerprint` — base64 Chromaprint string (the `AQAD…` prefix encodes algorithm v2)
- `meta` — the dial controlling response richness

The fingerprint covers only the first **120 s** (Chromaprint default `maxlength`) while
`duration` is the full length — duration is the match-tolerance key, fingerprint the acoustic
key; deliberately decoupled.

**Nature:** AcoustID is a **bridge table** (fingerprint cluster → MB recording ID) + a
**score**, *not* a metadata store. Descriptive text is pulled through from MusicBrainz.

**Response — `meta` dial:**

| `meta` | Adds per result |
|--------|-----------------|
| `recordings` | MB recording IDs + title + artist |
| `recordingids` | bare recording MBIDs |
| `releases` / `releaseids` | releases containing the recording |
| `releasegroups` / `releasegroupids` | release-groups |
| `tracks` | track position/medium within releases |
| `sources` | submission count |
| `usermeta` | submitter tags |
| `compress` | gzip response |

Every result carries the **AcoustID cluster UUID** + **`score` (0.0–1.0)**.

**What the project requests:** `acoustid.match` → `DEFAULT_META=['recordings']` → yields
`(score, recording_id, title, artist)`; keeps `score ≥ 0.5` (max 5); then **chains to MB**
(`get_recording_by_id`, `includes=[artists, releases, isrcs, media]`) for release/country/
ISRC/track-count. Release detail is an MB follow-up, not part of the AcoustID reply.

**Computed fingerprints (this disc) — the query payloads:**

```
 # true_secs fp_chars  fingerprint[:48]
 1     338.0     2911  AQADtMqmyJIS4r0QihSuo9Gh--Bxojz4IRo-olOIHuc
 2     277.5     3563  AQADtGGSSVmSSQq0B3kUlISpZBusnBmqFz387MKxw
 3     296.2     3354  AQADtEmiSNKSSFGGh4ce9IyHRj1yKaD_Ilx0w
 4     272.1     3472  AQADtFGiLNFCSoG2I3aUD71HXDlO4g9y
 5     257.6     3254  AQADtE9GJVqIC8cN88H94CI15EcP_Tg
 6     293.1     3495  AQADtM-SRInY4PjxH3iQL4aWUwgfBQ
 7     177.0     3664  AQADtIm2xUvS4DvC55gy4RP-FPV2
 8     212.3     3503  AQADtMoUqVEiSUHzM3gPcyHx6w
 9     323.0     3470  AQADtEkSSUqUUAs2u9iNH88B
10     253.3     3026  AQADtEkYbYuEUjv849ILnZj1
11     311.9     2991  AQADtIsSOaKyJHiR5Byp
```

Full fingerprints saved to `/var/tmp/jt_fps.json`.

**Observations (no conclusion):**
- AcoustID returns a score + MB recording IDs and therefore **can name the recording** —
  unlike AccurateRip, which returns only checksums/confidence and names nothing.
- It identifies the **recording** (the audio performance), not the **pressing**; many
  releases share one recording.
- Via recording→releases it can yield release candidates (a potential source of a barcode /
  catalogue number, hence a *Discogs* key) — but only through the MB follow-up, not from
  AcoustID's own reply.

### Q5 — Query MusicBrainz with the Disc ID; report only the number of results

**Method:** `musicbrainzngs.get_releases_by_discid("6E_gWEkDHQ0ROLKhj.vyupe298I-")` (live),
exact-TOC-match endpoint.

**Result:** `release-count = 10` (10 releases returned).

**Observation (no conclusion):** the single disc-ID (one TOC fingerprint) maps to 10 distinct
MB releases — i.e. one physical track-layout is shared across 10 catalogued releases.

### Q6 — List the 10 results: MB release ID, what differs per release, and CD confirmation

**Method:** `get_releases_by_discid(..., includes=["artists","labels","recordings"])`;
extracted date/country/barcode/label+cat#/medium-format/disambiguation per release.

**Invariant across all 10:** title *The Joshua Tree*, artist U2, track layout (shared
disc-ID), and **medium = CD** (every release). Table shows only the varying fields.

| # | MB release ID | Differs by (era · country · label:cat# · barcode · edition) | CD? |
|---|---------------|------------------------------------------------------------|-----|
| 1 | `19fb4543-45ee-4ded-a07b-32568f6214b0` | 1987 · US · Island:90581-2 · **075679058126** (unique) | CD |
| 2 | `e08f21bf-e63e-31f7-9cc3-6aac550a382a` | 1987 · AU · Island/Phonogram:842 298-2 · 9399084229829 | CD |
| 3 | `9d990576-a20a-3faf-88db-73d6b6c9364e` | 1987-03-09 · GB · Island:842 298-2 / CID U2 6 · 042284229821 | CD |
| 4 | `fb8f25c1-b149-383e-a206-ad9d24a32487` | 1987 · US · Island:422-842 298-2 · 042284229821 | CD |
| 5 | `88172719-d07a-345e-9fe8-c51b361891d9` | 2007-11-20 · US · Interscope/Island/UMe:B0010286-02 · 602517509474 · 20th anniv | CD |
| 6 | `231109c9-8490-3818-87d2-624c9a1e9c69` | 2007-12-03 · GB · Mercury:1744939 · 602517449398 | CD |
| 7 | `c87b171c-8708-3e83-bb43-801c4bd26d4b` | 2007-12-03 · GB · Mercury:1750947 · 602517509474 · 20th anniv | CD |
| 8 | `d287c703-5c25-3181-85d4-4d8c1a7d8ecd` | 2007-12-07 · DE · Mercury:1750948 · 602517509481 · deluxe | CD |
| 9 | `731ddd8a-4d03-3bda-bb48-9e03c5f8e46c` | 1991-11-18 · ZA · Island:STARCD 5879 · 6001210568933 | CD |
| 10 | `aba9be96-5800-436c-a617-4899b3648159` | 1987 · XE · Island:CID U2 6 / 842 298-2 · 042284229821 | CD |

**Observations (no conclusion):**
- All 10 are CD. Two era clusters: six 1987 + one 1991 reissue vs three 2007 anniversary/
  deluxe editions (a master-level split).
- Barcode collisions: `042284229821` is shared by #3 (GB), #4 (US), #10 (XE); `602517509474`
  by #5 (US) and #7 (GB). A known barcode would narrow, not uniquely pick, a release.
- Only #1 has a barcode unique within this set (`075679058126`).

### Q7 — Query AccurateRip with the disc ID; how many blocks?

**Method:** `_fetch_ar(11, "0015190b", "00b48124", 0x8a0bc50b)` + `_parse_dbar` (live, HTTPS).

**Result:** transport=https, 2464-byte body, **22 blocks**.

(Per-block confidences/CRCs already dumped in Q3.)

### Q8 — Cross-reference the AR blocks against the 10 MB releases

**Method:** computed this disc's per-track v1/v2 CRCs via `verify_rip` at read_offset=30
(PX-716A) and at 0, then matched each block's per-track `crc` against ours.

**Empirical anchor (our bytes vs blocks):**

| read_offset | result |
|-------------|--------|
| **+30** | block 0 = all 11 via **v2** (conf 200); block 1 = all 11 via **v1** (conf 200) |
| 0 | 0 blocks match (offset must be applied) |

Our rip falls in the **highest-confidence CRC group** (blocks 0 & 1 are the v2 and v1 records
of the same rip).

**The cross-reference (the actual question):**

| The 22 AR blocks | The 10 MB releases |
|------------------|--------------------|
| CRC groups = master × drive-offset × ripper-version | catalogue editions = country × label × barcode × era |
| our bytes → conf-200 group (blocks 0+1) | consistent with the 1987-master subset, indistinguishable within it |
| header = `(n_tracks,id1,id2,cddb_id)` + CRCs only | no AR CRC / disc-ID field |
| **no MBID / barcode / catalogue field** | **→ no shared key → no direct join** |

**Observations (no conclusion):**
- AR block headers carry only disc-level TOC quantities (identical across all 10 releases) +
  per-track CRCs. No release-identifying field exists, so blocks cannot be joined to MB
  releases on any shared key.
- Relationship is many-to-one and mis-aligned: same-master pressings (1987 US/GB/XE/AU #1–4,
  #10) are byte-identical → collapse to ONE CRC group; one master at different offsets splits
  into several blocks. Block count (22) ≠ release count (10) by construction.
- Our conf-200 match anchors the disc to one master (the 1987 family), but cannot pick among
  them (identical audio). The 2007 releases are a different master → different blocks, which
  we did not match.

### Q9 — How many releases are in the 1987 cluster?

**Method:** count by `date` year from the Q6 table (no new query).

| Era | Count | Releases |
|-----|-------|----------|
| **1987** | **5** | #1 (US), #2 (AU), #3 (GB), #4 (US), #10 (XE) |
| 1991 (ZA reissue) | 1 | #9 |
| 2007 | 4 | #5, #6, #7, #8 |

**Answer: 5 releases dated 1987.**

**Correction to Q8:** Q8 said "three 2007 remasters (#5, #7, #8)" — wrong; #6 (2007-12-03 GB,
Mercury 1744939) is also 2007, so the 2007 cluster is **4**. Totals now reconcile to 10.

**Observations (no conclusion):**
- The 1987 master family is *at least* these 5 (byte-identical same-master pressings our
  conf-200 CRC group is consistent with).
- #9 (1991 ZA) is a separate-year reissue that *may* reuse the 1987 master (→ family of 6),
  but is unverified — we have not ripped it.

### Q10 — List the 5 1987-cluster releases, highlighting which fields differ

**Method:** Q6 data, restricted to the 5 releases dated 1987.

**Constant across all 5:** title, artist (U2), era (1987), CD format, track layout, Island
label. Only the fields below vary.

| # | MB release ID | Country | Date | Barcode | Label : Cat# |
|---|---------------|---------|------|---------|--------------|
| 1 | `19fb4543-45ee-4ded-a07b-32568f6214b0` | US | 1987 | `075679058126` (unique) | Island : 90581-2 (unique) |
| 2 | `e08f21bf-e63e-31f7-9cc3-6aac550a382a` | AU | 1987 | `9399084229829` (unique) | Island/Phonogram : 842 298-2 |
| 3 | `9d990576-a20a-3faf-88db-73d6b6c9364e` | GB | 1987-03-09 | `042284229821` (shared) | Island : 842 298-2 / CID U2 6 |
| 4 | `fb8f25c1-b149-383e-a206-ad9d24a32487` | US | 1987 | `042284229821` (shared) | Island : 422-842 298-2 |
| 10 | `aba9be96-5800-436c-a617-4899b3648159` | XE | 1987 | `042284229821` (shared) | Island : CID U2 6 / 842 298-2 |

**Observations (no conclusion):**
- Country splits but not uniquely (#1 and #4 are both US).
- Barcode is unique for #1 and #2; #3/#4/#10 share `042284229821`.
- Cat# is the most granular (4 distinct strings) but #2 and #3 both carry `842 298-2`.
- Date precision: only #3 has a full day (`1987-03-09`).
- No single field uniquely keys all 5; only the combination (country + barcode + cat#) does.
  #3/#4/#10 separate only by country + cat#-suffix (CID U2 6 for GB/XE vs 422- for US).

### Q11 — Query AcoustID with the track fingerprints; how many recordings match?

**Method:** `acoustid.lookup(api, fp, full_duration, meta=["recordings"])` per track
(fingerprints from `/var/tmp/jt_fps.json`, durations from TOC). Live.

| # | dur | clusters | distinct recordings | top score | top recording |
|---|-----|----------|---------------------|-----------|----------------|
| 1 | 338 | 2 | 17 | 0.992 | Where the Streets Have No Name |
| 2 | 277 | 2 | 15 | 0.976 | I Still Haven't Found What I'm Looking For |
| 3 | 296 | 3 | 22 | 0.990 | With or Without You |
| 4 | 272 | 3 | 7 | 0.992 | (Star Spangled Banner /) Bullet the Blue Sky |
| 5 | 258 | 4 | 5 | 0.997 | Running to Stand Still |
| 6 | 293 | 3 | 6 | 0.997 | *None* (null title in MB) |
| 7 | 177 | 3 | 6 | 0.984 | In God's Country |
| 8 | 212 | 4 | 2 | 0.977 | Trip Through Your Wires |
| 9 | 323 | 3 | 4 | 0.995 | One Tree Hill |
| 10 | 253 | 3 | 7 | 0.989 | Exit |
| 11 | 312 | 2 | 6 | 0.991 | Mothers of the Disappeared |

**Answer: 2–22 recordings per track; 97 unique recordings total.**

**Observations (no conclusion):**
- Not one recording per track. The fingerprint matches every MB recording entity sharing the
  audio; MB holds many near-duplicate recordings for a famous track (1987 master across
  releases + remaster recordings + comp/box entries). Inflation is MB modelling, not acoustic
  ambiguity — top scores 0.97–0.997.
- Per-cluster count is cleaner: each track is 2–4 AcoustID clusters (track 1's 17 recordings
  sit under 2 clusters).
- Quirks: track 6 top recording has a null title (should be *Red Hill Mining Town*); track 4
  top hit is a medley title. Other 9 top hits reproduce the album tracklist in order.

### Q12 — MB shows different release lengths (50:08 vs 50:14). Compute running time locally and match.

**Method:** computed total time from the TOC several ways; the MB-faithful definition is the
sum of consecutive INDEX-01 track lengths = `leadout − track1_INDEX01`.

```
A full disc (frame0→leadout)         226047 fr = 50:13.96
B play total (t1 idx01→leadout)      226015 fr = 50:13.53  ← MB-faithful
C sum durations (excl pregaps)       225907 fr = 50:12.09
D MB-style sum (consecutive INDEX01) 226015 fr = 50:13.53 ≈ 50:14
```

| MB release | MB Length | vs ours (50:13.5) |
|------------|-----------|-------------------|
| **042284229821** (GB #3 / US #4 / XE #10) | 50:14 (3014 s) | **match** (Δ≈0.5 s) |
| 9399084229829 (AU #2) | 50:08 (3008 s) | no (Δ≈5.5 s) |

**Answer: our running time (50:13.5 ≈ 50:14) matches `042284229821`, not the AU
`9399084229829`.**

> **⚠ CORRECTED BY Q16.** The "50:08 = AU candidate #2" attribution below is WRONG. The
> 50:08 belongs to a *different* release (`82bcd715`/`e4d4017b`) that shares barcode
> `9399084229829` but does NOT carry our disc-ID — it was never a candidate. Our actual #2
> (`e08f21bf`) sums to 50:13.53 and matches us. Running time excludes NONE of our disc-ID
> candidates. See Q16.

**Observations (no conclusion):**
- A release's MB "Length" is editorially-stored track-time data, *independent* of the attached
  disc-ID — so this is a real secondary check even though all 10 share our disc-ID.
- ~~This is the FIRST signal to discriminate within the 1987 cluster~~ — **WRONG, see Q16.**
- ~~The AU #2 (50:08) is 5.5 s short~~ — **WRONG attribution, see Q16.**
- Definition matters: the excl-pregap sum (C, 50:12) would mis-match; the MB-faithful
  leadout−track1 (D, 50:14) matches.

### Q13 — Check the 042284229821 group against per-track lengths; double-check runtime + per-track of US 075679058126

**Method:** `get_release_by_id(..., includes=["recordings"])` per release; compared MB's
per-track lengths (ms) against our local per-track lengths (D=consecutive-INDEX01, C=excl-pregap).

**Per-track match (seconds):** all four releases are identical to each other and to our **D**:

```
 #   our D    our C  |  #1     #3     #4    #10   (MB, seconds)
 1  337.97  337.97   | 337.97 337.97 337.97 337.97
 2  278.13  277.49   | 278.13 278.13 278.13 278.13   ← MB = our D, not C
 6  293.93  293.13   | 293.93 293.93 293.93 293.93   ← MB = our D, not C
 …  (tracks 3,4,5,7,8,9,10,11 all match exactly)
 total 50:13.53      | 50:13.53 (all four)  n=11 each
```

**Answers:**
- **042284229821 group (#3, #4, #10): per-track lengths match ours exactly** (D convention),
  all three identical.
- **#1 US 075679058126: runtime 50:13.53 and all 11 per-track lengths match ours exactly too**
  — #1 is equally consistent; runtime does NOT exclude it.

**Observations (no conclusion):**
- MB stores per-track times in the consecutive-INDEX-01 convention (our D): tracks 2 & 6 prove
  it (MB = 278.13/293.93 = D, not the excl-pregap C = 277.49/293.13). Validates Q12's def D.
- Per-track lengths do NOT split this group — #1/#3/#4/#10 are byte-identical timing (same TOC).
- Timing-consistent 1987 set is now {#1, #3, #4, #10}; AU #2 (50:08) excluded. These four differ
  only by barcode/cat#/country (Q10), which no single attribute resolves without knowing our
  disc's own country — for which we have no on-disc source (no MCN).

### Q14 — What disambiguation data do we have so far to exclude the 2007 releases?

**Method:** fetched per-track + total lengths for all four 2007 releases (#5,#6,#7,#8) — the
11-track album medium — and compared to ours; then inventoried every signal collected.

**Empirical result:** all four 2007 releases have **identical** per-track lengths and total
(50:13.53) to ours — **max |Δ| = 0.00 s**. The remaster kept the exact track layout.

| Signal | Excludes 2007? | Evidence |
|--------|----------------|----------|
| Disc-ID (TOC) | No | all four matched our disc-ID (same TOC) |
| Total runtime | No | 50:13.53, identical |
| Per-track lengths | No | Δ = 0.00 s every track |
| AcoustID fingerprint | No | Chromaprint robust across remasters by design; returns both eras |
| **AccurateRip CRC** | **Only candidate** | different master → different audio → different block; our bytes in conf-200 group |

**Answer: the only datum that can exclude the 2007 releases is the AccurateRip CRC.**

**Observations (no conclusion):**
- Everything TOC-derived (disc-ID, durations) is blind to remastering (layout unchanged);
  Chromaprint is deliberately remaster-robust. Only a bit-exact checksum sees the difference.
- AR does NOT label eras: it proves our disc is one master and not the others, but on its own
  cannot name which. The "it's 1987" anchor is circumstantial — our match is the highest-
  confidence, longest-circulating group (conf 200); a newer 2007 remaster would carry far
  lower confidence. Strong, not airtight.

### Q15 — Fetch full MB records for the final 4 candidates (one file each); table all differences

**Method:** `get_release_by_id(..., includes=[artists,artist-credits,labels,recordings,
release-groups,discids,media,isrcs,aliases,annotation,url-rels])` per candidate. Saved to
`private/research/incoming/disambiguation/{01_US…,03_GB…,04_US…,10_XE…}.json` (30–51 KB each).
Flattened all fields to dotted paths and diffed (667 differing paths; most are per-track IDs).

**Invariant across all 4:** title, artist, all 11 track titles, recording IDs, ISRCs,
per-track times (Δ 0.00 s), release-group `6f3e9fa6`, status Official, quality normal,
language eng/Latn, format CD, 11 tracks.

**All meaningful differences:**

| Attribute | #1 US | #3 GB | #4 US | #10 XE |
|-----------|-------|-------|-------|--------|
| MB release id | `19fb4543` | `9d990576` | `fb8f25c1` | `aba9be96` |
| barcode | 075679058126 | 042284229821 | 042284229821 | 042284229821 |
| date | 1987 | 1987-03-09 | 1987 | 1987 |
| country/area | US | GB | US | XE/Europe |
| label : cat# | Island:90581-2 | Island:842 298-2 / CID U2 6 | Island:422‐842 298‐2 | Island:CID U2 6 / 842 298-2 |
| packaging | (none) | Jewel Case | Jewel Case | Jewel Case |
| asin | — | B000001FS3 | B000001FS3 | — |
| cover-art count | 16 | 4 | 11 | 9 |
| Discogs link | rel/370670 | rel/1198146 | rel/440625 | rel/5120863 |
| other DB links | — | RateYourMusic | — | — |
| annotation | vinyl + do-not-merge | do-not-merge | (none) | detailed jewel-case note |

**Observations (no conclusion):**
- MB→Discogs bridge found: each release `url-relation` points to a distinct Discogs release
  (370670 / 1198146 / 440625 / 5120863) — the Discogs key Q2 said we lacked, no barcode guess.
- Recording layer is shared: same recording IDs + same ISRCs (GB-prefixed GBAAN87900xx /
  GBUM707097xx) across all 4 → ISRC cannot disambiguate here.
- Annotation corroborates the AU exclusion: #1/#3 warn "tracks 9 & 10 have different track
  times from the other 11-track release… do not merge" — almost certainly the AU #2 (50:08).
- Individually weak: barcode isolates only #1; ASIN pairs {#3,#4} vs {#1,#10}; country splits
  but #1/#4 both US; only the full tuple separates all four.

> **⚠ Finalist set is 5, not 4 (see Q16):** AU #2 (`e08f21bf`) was wrongly excluded in Q12
> and should be included. Q15 fetched only 4 full records; #2 should be added for completeness.

### Q16 — Confirm the "tracks 9 & 10 differ" annotation vs our earlier track-time match

**Trigger:** the #1/#3 annotation says "tracks 9 & 10 have different track times from the other
11-track release… do not merge," but Q13 found all candidate track times matched. Apparent
contradiction.

**Method:** searched `search_releases(barcode="9399084229829")`; inspected disc-IDs and
per-track times of every release sharing that barcode.

**Findings:**
- Barcode `9399084229829` is shared by **4 distinct MB releases**:

  | release | total | t9 | t10 | has our disc-ID? |
  |---------|-------|-----|-----|------------------|
  | `e08f21bf` (our #2) | 50:13.53 | 323.0 | 253.3 | **yes** |
  | `82bcd715` | 50:07.93 | 283.9 | 292.9 | no |
  | `e4d4017b` | 50:07.93 | 283.9 | 292.9 | no |
  | `499dc1b1` | 50:13.00 | 283.0 | 293.0 | no |

- The **50:08** value = `82bcd715`/`e4d4017b` — different track 9/10 split, **different
  disc-ID**, never a candidate. This is the "other 11-track release" the annotation warns about.
- Our actual #2 (`e08f21bf`) sums to **50:13.53** and matches us — and has **33 disc-IDs**
  attached (spanning 50:08–50:18), our `6E_gW…` among them.

**Resolution / CORRECTION:**
- **No contradiction in candidate data** — all releases carrying our disc-ID match our track
  times (50:13.53), tracks 9 & 10 included. The user's challenge was correct.
- **Q12/Q13 error:** I attributed the web "50:08" to candidate #2. Wrong — it's a non-candidate
  release with the same barcode. **Running time excludes NONE of our disc-ID candidates;** the
  finalist set is **{#1, #2, #3, #4, #10}** (5).

**Observations (no conclusion):**
- One MB release aggregates many disc-IDs (e08f21bf: 33). A release's "Length" = track-list
  sum, *separate* from any single disc-ID's TOC length; same barcode spans timing-variant
  releases. MB running time is a weak discriminator among same-disc-ID candidates.
- Running time CAN separate releases with genuinely different track-lists (the 50:08 variants),
  but those weren't candidates to begin with (different disc-ID).

### Q17 — Confirm disc-ID, total time, per-track time, AR checksums are identical across the 5

**Method:** measured items 1–3 from the 5 saved records; reasoned about item 4.

| Property | Result |
|----------|--------|
| 1. MB Disc-ID | ✅ our `6E_gW…` attached to all 5 (disc-ID *sets* differ: 34/33/35/35/1) |
| 2. Total running time | ✅ 3013.53 s (50:13.53), identical |
| 3. Per-track running time | ✅ all 11 identical |
| 4. AccurateRip checksums | ⛔ **NOT confirmable** — see below |

**Item 4 — why it cannot be confirmed:**
- AR checksums are not a MusicBrainz property; MB stores no audio/AR data. Cannot be read from
  these records at all (unlike 1–3).
- We hold one physical disc; we measured *its* CRCs (conf-200 group), not 5 separate pressings.
- Identical timing ≠ identical audio (the 2007 remaster proved this in Q14). So matching 1–3
  does NOT imply matching AR checksums.
- Inference only: IF all 5 are the same 1987 master, audio is byte-identical → same AR
  checksums (and AR could not tell them apart anyway). Likely for original-era pressings, but
  not a confirmation.

**Observations (no conclusion):**
- The dividing line of the whole interrogation: items 1–3 live in the TOC/catalogue layer that
  every release shares (all confirmable, all identical); item 4 is the one quantity that lives
  in the audio — absent from MB, requiring the physical disc, and the only thing that ever
  separated masters (1987 vs 2007).

### Q18 — Research note: CUETools and the multi-pressing cross-check (no disc query)

**Method:** web research — cue.tools wiki + Hydrogenaudio.

**CUETools** = lossless audio conversion + verification suite. Two verification innovations:

**(a) AccurateRip offset-finding ("the mathematical solution").** AR v1 checksum is
Sum(sample_i x i) — linear in sample position — so the checksum at offset N±1 derives from the
checksum at offset N by adding/removing the boundary samples, not recomputing. CUETools sweeps
the whole ±offset range (largest known DB offset ~1776 samples) in ~one pass, so an
uncorrected rip can still be matched by finding its offset. (Same mechanism behind
"autodetected offset", which CLAUDE.local warns is unreliable at low confidence.) Does not
convert v1<->v2; v2 (fixes v1's ~3% right-channel under-count) is stored separately.

**(b) CTDB — the CUETools Database (separate from AccurateRip).** Per disc stores:
TOCID (hash of TOC); CTDBID = CRC32 of whole disc (excl HTOA + 5880 samples each end);
16-byte offset-finding checksum; **~180 KB parity recovery record** (≈2x for popular discs) —
Reed-Solomon-style parity that lets CUETools *repair* a damaged rip, not just detect; metadata
(date, artist, title, drive model, IP). Defining property: **offset- and pressing-agnostic by
design** — "different pressings of the same CD are treated as the same disc; it doesn't care."
Confidence 2/X+ = probably undamaged; outcomes correct / correctable / uncorrectable. Slower
than AR, full-disc rip, ~200 KB/lookup.

**Relevance to disambiguation (observation, no conclusion):**
- CTDB is the WRONG tool for telling pressings apart: it is *engineered* to discard the
  offset/bit-exact signal that let AR separate our 1987 master from the 2007 remaster (Q8/Q14).
  Adds repair + robustness, not disambiguation power.
- AR's offset-*sensitivity* is the discriminator; CUETools' work largely *erases* that
  sensitivity for cross-drive/cross-pressing robustness.
- New-to-us idea: CTDBID is a single whole-disc CRC32 (one value vs AR's per-track blocks) —
  but still keyed to the audio master, not the catalogue pressing.

Sources: cue.tools/wiki/CUETools_Database; cue.tools/wiki/CUETools;
wiki.hydrogenaudio.org AccurateRip; github sambhare/CueTools.NET AccurateRip.cs.

---

## 4 — Proposal: final disambiguator (user, 2026-06-19)

The interrogation hit the brick wall it was meant to find: **byte-identical same-master
pressings cannot be identified from the disc — even with on-disc metadata** — they differ only
in packaging/catalogue. So the final stage is necessarily a *preference*, not an identification.

### Proposed mechanism
1. **Plurality barcode** — the most common normalised barcode scores highest (here: the
   `042284229821` trio #3/#4/#10).
2. **`preferred_country` config** — comma list of MB 2-letter codes, e.g. `"[GB, XE, US]"`. A
   **priority ranking, not a filter**: listed countries rank in order; every unlisted country
   gets the same lowest priority. Nothing is excluded — only scored.
3. Output of the MB pipeline is a **scored candidate set** (no discards) feeding the larger
   consensus model, which selects the final winner.

### Discussion / refinements (advisor-reviewed)
- **Discard-vs-score contradiction — resolved toward pure scoring.** "Plurality leaves 3
  candidates" (hard cut) conflicts with "nothing discarded, only scored." Adopt **pure
  lexicographic scoring**: barcode-plurality is the top sort key (the trio tie for the lead),
  country the next key, #1/#2 retained at lower rank. **Why:** a hard barcode cut would drop #1
  — the *only* uniquely-barcoded release (the one an on-disc MCN could pin) — and would
  eliminate it *before* a `preferred_country = US` setting could ever rescue it. A popularity
  signal must weight, never gate.
- **Code placement (refine, not replace).** This is the last-resort rung of the existing
  selection cascade `_disambiguate_by_isrcs` → `_resolve_via_isrc_tally` →
  `duration_match_lookup` (all return None here: shared ISRCs, identical durations). It ranks
  **below** ISRC/duration (recording identity > catalogue popularity) and **replaces the
  `candidates[0]` silent default** in `_pick_canonical_mcn` (cdda2img.py:1049).
- **D4 verdict (the worked example trust_model_design.md §9.4 asked for):** catalogue data
  **cannot identify** the pressing but **enables a deterministic, user-controllable,
  reproducible preference** among indistinguishable candidates. Persist (D4=persist) is
  justified **for labelling + provenance, not disambiguation power** — it sharpens the *label*,
  not the match.

### Refinements (user, 2026-06-19)

**Pure lexicographic scoring CONFIRMED.** Sort-key chain (each only breaks ties left by the
key above):

| Rank key | Type | Notes |
|----------|------|-------|
| 0. on-disc MCN match | *objective* | if a real MCN is read (rare), matching candidates rank top — we *know* the barcode; popularity irrelevant |
| 1. barcode-plurality | popularity prior | common no-MCN case; most-catalogued normalised barcode |
| 2. preferred_country | user preference | ordered list; unlisted = lowest equal |
| 3. age (earliest date) | arbitrary fallback | |
| 4. MB release-ID | terminal | stable, reproducible winner |

- on-disc MCN above plurality = evidence outranks proxy (mirrors trust model `OBJECTIVE >
  everything`). MCN and plurality fill the *same* slot (barcode determination), one objective
  one prior.
- `#4 > #1` confirmed: #4 is in the plurality tier, #1's barcode is a singleton → #4 outranks
  #1 at the barcode key; country never arbitrates them.
- **Endorsed consequence:** barcode-above-country systematically ranks a region-specific
  *uniquely-barcoded* pressing (#1) *below* the common-barcode tier, even for a matching
  `preferred_country`. Defensible as a population prior; preferred_country only arbitrates
  *within* a barcode tier.

**Expanded stored/displayed metadata (D4=persist scope, spec-before-code):**

| Field | Current state | Action |
|-------|---------------|--------|
| year of *this* release | ✅ stored as `release_date` (rbi_format.py:315) | **display gap only** — list/catalogue show `original_release_*` but not `release_date`; just render it (no spec change) |
| label catalogue number | ⚠️ `disc_id` field exists (PTI 0x86 CD-Text ref, CD-Text-only) | add `catalog_number` from MB/Discogs; **rename `disc_id`** (collides with "MB Disc ID" + `mb_release_id`) e.g. `cdtext_catalog_ref` |
| label (e.g. Island) | ❌ missing | add `label` |
| country | ❌ missing | add `country` |

`catalog` (MCN/EAN-13 barcode) and `catalog_number` (label's own number, e.g. `CID U2 6`) are
distinct identifiers → separate fields (both vary independently across the 5).

### Open sub-questions
- **Terminal tiebreak:** earliest date, then MB release-ID (deterministic).
- **Provenance:** `preferred_country` makes output config-dependent → record applied preference
  in PROV (R10 reproducibility).
- **Counting universe:** resolved into the provider-role model below — MB-internal count is the
  selector; cross-source (MB+Discogs) count sharpens *field confidence*, not selection.

### Provider-role model (answers "what do non-MB providers contribute to disambiguation?")

Disambiguation is a **cascade through granularities**; each provider operates at one:

| Provider | Keyed by | Granularity | Role | Weight |
|----------|----------|-------------|------|--------|
| TOC / local | computed disc-ID | disc (universe) | **defines candidate set** | foundational |
| AccurateRip | TOC disc-ID | **master/era** | **excludes wrong masters** + rip correctness | high but coarse (binary in/out) |
| MusicBrainz | disc-ID | release | **enumerates releases + supplies catalogue fields** | primary substrate |
| AcoustID | PCM fingerprint | recording | track labelling + gross-mismatch sanity | ~0 for editions (remaster-robust) |
| Discogs | MB→Discogs url-rel (per candidate) | release | **corroborates** catalogue fields | corroboration only, not a selector |
| CDDB | TOC cddb-id | disc | free-text fallback | ~0 for editions |

**Conclusion:** for byte-identical pressings, non-MB providers contribute ~nothing to
edition-level disambiguation — AR works upstream (master exclusion), AcoustID/CDDB are
edition-blind, Discogs only corroborates. **The edition choice rests on MB catalogue data +
preference config.** This is the ceiling of what's knowable, not a fixable gap.

**Precedence = granularity order:** (1) TOC defines set → (2) AccurateRip narrows master →
(3) ISRC/duration narrow recording/timing *when discriminating* → (4) catalogue scoring
(barcode-plurality → preferred_country) picks a *preference* → (5) age/MBID terminal.

**Discogs weight (with the url-rel bridge):** per-candidate corroborator — does Discogs agree
with MB on this candidate's label/cat#/country/year? Agreement raises field confidence;
disagreement is surfaced. Can also feed cross-source barcode plurality (count MB *and* Discogs)
— sharpens field confidence, not the selection.

### Decisions locked (user, 2026-06-19)

- **Barcode > country (preponderance of evidence).** The whole operation is a sophisticated
  guess; absent irrefutable truth, the most defensible guess is the preponderance of evidence
  → barcode-plurality is the top prior, country subordinate. CONFIRMED.
- **(a) Discogs selection-weight — PENDING TEST.** Built `tools/compare_mb_discogs.py`: joins
  each MB release to its linked Discogs release (url-rel) and tallies field mismatch. First
  sample (the 5 finalists): **barcode 0/4, label 0/5 (perfect); country 2/5; catalog_number
  3/5 (worst); year 1/2**. Finding: barcode/label are reliable cross-source; catalog_number/
  country are noisy (services pick different *valid* cat#s; country vocab differs + substance
  e.g. MB "GB" vs Discogs "Europe"). So if Discogs gets selection weight it should corroborate
  **barcode/label only**. n=5 single-album — needs a broad corpus before any change.

  **Larger sample (45 diverse albums, `--sample` mode, ~35 compared after skips):**

  | Field | Mismatch | Reading |
  |-------|----------|---------|
  | barcode | **0/30** | authoritative — zero cross-source disagreement at scale |
  | year | 3/27 (~11%) | low; genuine edition-date differences |
  | catalog_number | 6/35 (~17%) | mixed: barcode-as-catno in MB (88691975242 vs GET 9006 — R1 catches it), disambig suffixes (ZD72131 vs ZD72131(2)), genuinely different valid cat#s |
  | label | 7/35 (~20%) | parent/sub-label + reissue-label granularity |
  | country | 19/35 (~54%) | **mostly vocabulary, not substance** |

  - **Barcode is the gold standard** (0/30) → Discogs corroboration should ride on barcode above all.
  - **Country 54% is an artefact:** MB uses ISO-3166; Discogs uses display names + multi-region
    strings (CA/Canada, AU/"Australia & New Zealand", GB/"UK & Europe"). Substantive country
    disagreement is only ~5–6/35 (GB vs Europe, US vs Europe, one mislinked FI vs Argentina).
    Discogs country is a coarser, incompatible vocabulary — needs an ISO map, fuzzier anyway.
  - **Methodology caveat:** a few MB→Discogs url-rels are loosely matched (FI vs Argentina,
    Columbia vs XL) — the bridge is good but not infallible.

  **Refined (a) conclusion:** Discogs corroboration value = **barcode (rock-solid) > year >
  catalog_number/label (moderate) > country (vocabulary-incompatible)**. If Discogs gets
  selection weight, base it on **barcode agreement**, never country; run catalog_number through
  the reject-battery first. Still pending a final decision, but the evidence now favours
  *barcode-only* Discogs corroboration.
- **(b) AcoustID = GATE, not selector.** "Checksums don't lie." AcoustID corroborates *whether
  the audio matches the claimed album at all* (catches a wrong disc-ID/TOC collision or
  mispress); it has ~0 power to pick an edition. Formalise as a gate over the candidate set,
  separate from the selection cascade.
- **(c) `preferred_country` = TOML array:** `preferred_country = ["GB", "XE", "US"]`. Default
  unset/empty ⇒ skip the country key (straight to age), no hidden bias.
- **(d) Rename `disc_id` → `cdtext_catalog_ref` + spec bump.** Current `disc_id` (PTI 0x86
  CD-Text label ref) collides with "MB Disc ID" / `mb_release_id`. Rename + add `catalog_number`
  (from MB/Discogs). spec-before-code (`rbi_spec.md` bump + migration).

### Metadata presentation invariants (user, 2026-06-19)

While expanding stored/displayed metadata, enforce (implies **one canonical renderer** shared
by the metadata menu, `catalogue`, and `list`):

1. **No data gaps** — everything stored is displayed, and everything displayed is stored.
2. **Displayed everywhere** — the same field set appears in the metadata menu (creation landing
   page), `catalogue`, and `list`.
3. **Identical format** — same spacing, order, labels across all three, so a user recognises
   the catalogue entry from the `list` output and from the original creation menu at a glance.

### `catalog_number` sanitisation (research, 2026-06-19)

Full report: `private/research/incoming/cd-label-catalogue-codes.md` (+ verbatim Discogs
guideline extracts `discogs_baoi.txt`, `discogs_catno.txt`, `discogs_price_distribution_codes.txt`).

Root cause of the noisy `catalog_number` (Q15, compare_mb_discogs 3/5 mismatch): a CD carries
~a dozen codes and contributors submit the wrong one. **No universal positive cat# format
exists** → sanitise by a **reject-battery**, not a format match:

- **Hard reject** (negligible FP): R1 barcode (`^\d{12,13}$` + GS1 check — *reuse
  `validators.is_valid_gtin13`*), R2 SID (`contains IFPI`), R3 Label Code (`^LC[\s-]?\d{4,5}$`),
  R5 SPARS (`DDD/ADD/AAD/…`), R6 rights-society token list, R7 depósito legal (`^D\.?L\.?`).
- **Conditional**: R4 ASIN (`^B0[0-9A-Z]{8}$`) — reject unless label is Universal.
- **Rank, never reject** (the user's "discount codes" = Price Codes [Discogs 5.2.g] +
  Distribution Codes [PolyGram PY/PG, EMI…]): R8/R9 — *visually identical to real cat#s, ~8
  disjoint forms, the hardest impostor*; demote below a cleaner candidate, don't drop.
- **Tiebreaks**: prefer Discogs's **first** cat# (guideline 4.8.4 — best matches the label's
  numbering system); weak positive shape = ≥1 letter-group + ≥1 digit-group (advisory only).
- **Last resort**: keep a flagged low-confidence value rather than discard ("Guess the Album").

This is the concrete spec for the new `catalog_number` field's ingest filter (feeds B6/§10).

### Fit with `trust_model_design.md`
- Fills a real gap: the trust model (§2) resolves per-*field* but has **no release-selection
  step** for multiple disc-ID matches with differing catalogue values. This is **Layer 1
  (release-selection)**; the trust model is **Layer 2 (per-field)**.
- Answers §9.4's open equal-trust tiebreak: all 5 candidates are equal `DISC_ID` trust; this
  cascade is the tiebreak (ISRC → duration → barcode-plurality → preferred_country → terminal).
- Fits bundle **(ii)-minimal + B6-persist**, not the B4 rewrite. `preferred_country` is not a
  trust level (those rank source reliability) — it's an orthogonal user-preference prior at
  release-selection. (No D1 decision taken; no §10; no code — standing constraint holds.)
