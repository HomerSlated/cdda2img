# Flagged-span recovery rung — plan

Status: **PLAN, no code** (2026-09-16). Owner: cdda2img. Engine: AccuDisc ≥ 0.41.0.
Hardware tests on the LITE-ON LH-20A1S (`/dev/sr0`) are coordinated with AccuDisc under
the agreed protocol (our §198, their 2026-09-16d). The PX-716A stays under the no-test
rule until it is repaired.

## 1. Why

The Tracy Chapman rip on the LITE-ON with AccuDisc 0.39.0 verified 8/11. Tracks 8, 9 and 11
never matched in **18 whole-track attempts each** (`track-ladder`: 40X down to 4X, 3 passes),
and CTDB parity was declined at the AccurateRip gate (`ctdb_offset=-663`, most likely a
different pressing, so CTDB is not a dependable second exit on this disc).

Scored against the PX-716A container (11/11, confidence 200), the damage is sparse:
4 / 23 / 6 stored sectors, mostly tens of bytes each (list in AccuDisc correspondence §196).

**A whole-track attempt must get every site right in the same pass.** Track 9 has ~20
sites. AccuDisc's locator read (2026-09-16c, `--verify 3`) got 13 of 32 sites exact while 23
*other* sectors came back wrong, so each site succeeds on some passes and not others. A
whole-track attempt multiplies those odds; a per-site accumulation adds them. That is the
whole argument for this rung, and it is arithmetic, not yet a measurement.

## 2. What is already measured (offline, 2026-09-16)

AccuDisc's locator capture (engine 0.40.0, `read --verify 3 --c2f --map-file`, four raw spans,
813 raw sectors), scored per **raw** sector against the PX-716A key
(raw sector R = stored bytes `R*2352 - 24`, read offset +6):

| delivered copy | right | wrong |
|---|---|---|
| own C2 all-zero | 770 | **4** |
| own C2 fired | 0 | 39 |

The four C2-clean wrong sectors:
- raw 113069–113071: **exactly +96 bytes (24 samples) late**, map state `RECOVERED`. The
  self-confirming slip AccuDisc fixed in 0.41.0 (verify/seam/rereads) and is fixing for
  `c2_retries` in 0.42.0 (`953b991`, pushed 2026-09-16; its drive verification, AccuDisc's
run B, was in progress when this was written).
- raw 162891: the disc's last sector. Its stored tail comes from the lead-out and AR
  excludes the final sectors. **A scoring artefact, not a target.**

So on this drive **"C2 fired on the delivered copy" never marked a right sector, and "C2
clean" was wrong only on a slip.** One pass, one disc, one drive: this justifies the
acceptance rule in §4 as a starting hypothesis, nothing more.

**Re-scored on AccuDisc 0.42.0 (their runs A and B, 19:08, same four spans; raw 162891
excluded; states decoded through the binding's `map_state`):**

| run | request | accepted right | **accepted wrong** | rejected wrong | rejected right |
|---|---|---|---|---|---|
| 16c | 0.40.0, `--verify 3` | 770 | 3 (the slips) | 39 | 0 |
| A | 0.42.0, `--verify 3` | 775 | **0** | 36 | 1 |
| B | 0.42.0, `--c2-retries 3`, no verify | 743 | **35** | 34 | 0 |

**Run B is the important one.** Its wrong accepted sectors are almost all in span 113068
and are **pure shifts of +48 or +96 bytes, marked plain `OK`, C2 clean, with the read
reporting `slips=0`.** A read without `verify_passes`/`overlap_sectors` has no slip detector,
so a slipped stream is indistinguishable from a clean one in every signal the engine
returns. That is not the `c2_retries` defect (0.42.0 closed that); it is the plain stream.
Two consequences:
- **The acceptance rule is only sound on reads that carry a position witness** — a second
  transfer of the same sectors, `verify_passes >= 2`. A copy from a read without one is
  never accepted (§4 step 4).
- AccuDisc's own scoring (2026-09-16f, stored-sector domain, C2 counted on raw N or N+1)
  gives A 0 wrong of 749 accepted and B 31 wrong of 755, with B's slips in the primary
  chunk transfers at 113068 and 113092. AccuDisc confirmed (2026-09-16g) that these
  and the raw-domain table above are **the same set** of sectors: a stored sector passes
  only when both raw N and N+1 do. In A the 113069–71 slip did not recur, so 0.41.0's
  neighbour anchor was **not exercised on hardware** by this run.
- **The capture pass has the same property.** The whole-disc read runs with neither
  verify nor overlap, so its `OK` sectors can be slipped too, and its damage lane cannot
  locate them. §4 step 1's blind spot is therefore measured, not hypothetical.

Map byte decode (header, `ACCUDISC_MAP_STATE` / `ACCUDISC_MAP_SEVERITY`): low nibble =
state, high nibble = severity (C2: ~log2 fired bits; RECOVERED: extra reads taken; SUSPECT:
~log2 disagreeing bytes). Decode through the binding's `map_state`, never by hand.

**Correction, 2026-09-18 (AccuDisc §199.3).** 16f/16g reported run B's Q as *blind* to the
displacement as though that had been measured. It had not: runs A and B captured no raw
subchannel at all, so the check never ran — Q was not asked, rather than asked and silent.
The conclusion is unchanged but now rests on mechanism instead of a measurement that did not
happen: the Q check is whole-sector, quantum 588 samples, against displacements of 12 and 24.
**Clean-media slip rate, stated with the right trial unit (AccuDisc §18c, bounded in
§200.2).** Six full-speed single-pass transfers of clean media at two radii delivered 0 wrong
sectors, with `slips` and `subq_misposition` both zero. The tempting summary — "0 of 24,000"
— is wrong, because **the fault displaces a transfer, not a sector**: run B's chunks at
113068 and 113092 went late as units. At `sector_len` 2742 under a 64 KiB cap the chunk is 23
sectors, so 4,000 sectors is 174 chunk-transfers and six runs are **1,044 trials**, not
24,000. Rule of three on zero events in 1,044 gives a **95% upper bound of ≈0.29% per
chunk-transfer**; counting sectors would have given 0.0125% and overstated the result by 23×.

So the defensible sentence is: *on clean media this drive slips at a rate bounded above by
roughly 0.3% of transfers (95%), on one disc at two radii — not zero, and untested on other
discs, other radii, and any span where C2 is silent but damage is present.*

That still supports §4.1's locator — every slip ever measured on this drive sits inside or
beside a C2-flagged span, and these spans carried `c2 = 0` throughout — but it **does not**
show that slips require C2. The locator's blind spot remains a slip in a span with no C2 flag
anywhere near it; the measurement bounds that case rather than eliminating it, and H2 must be
allowed to fail.

## 3. The constraint the design turns on: no absolute gate below a track

AccurateRip v1/v2 and CTDB CRCs are **per track**. The frame-450 CRC covers one frame. So:

1. **Span candidates are chosen by relative evidence only** (C2, map state, agreement across
   reads). Those are the engine's claims, and every one of them has been wrong at least once
   (§2).
2. **Assemble in memory, commit on pass.** The candidate track is built in a buffer and
   written to the raw PCM file only after `match_track_pcm` verifies it. This keeps the
   invariant the existing rung guarantees: *a track that never matches keeps its original
   audio; no unverified splice.* Writing spans into the file as they arrive would break it
   silently.
3. **No search.** With k candidates at ~20 sites, trying combinations against AR is k²⁰.
   Convergence comes from per-sector acceptance (§4), then one gate per assembled track.
4. **The PX-716A key is a bench instrument only.** The rung may not read it. It scores the
   rung offline and in tests.

## 4. Algorithm

Per AR-failed track, after CTDB and before `track-ladder`:

1. **Locate.** Targets = sectors inside the track's raw window whose capture-pass damage
   lane is set (map `C2`, `HARD` or `SUSPECT`; `disc_damage`, captured on every rip).
   Exclude the disc's final sector (§2).
   - *Known blind spot:* the capture pass has no verify/overlap, so it cannot see a slip.
     A slip outside every padded span is found by nothing here; the AR gate still rejects
     the track, and `track-ladder` still runs after.
2. **Cluster.** Merge targets closer than `G` sectors, pad each span by `P` sectors (the
   0.41.0 neighbour anchor needs neighbours with alignment signal), clamp to the track
   window plus the offset margin. `G`, `P` are profile fields, tuned offline on the maps.
3. **Re-read the spans**, one pass = each span once, **speed-diverse across passes**
   (the bound ladder, as today). This is the engine author's own advice, not merely ours:
   *"Verify passes themselves stream at `speed_x` — drives recalibrate on every speed
   change, so per-chunk speed switching thrashes; run whole-range passes at different
   `speed_x` yourself for a full speed-diverse sweep"* (`accudisc.h`, `speed_ladder`). So
   `speed_ladder` and the per-pass `read_speed` are different mechanisms and both belong.
   Request: PCM + C2 + **raw subchannel** + `status_map` + `subq_map`; `verify_passes=2`,
   `overlap_sectors=4` (R3, cdda2img RECOVERY.md §4.2); **`c2_retries=0`** until 0.42.0's
   fix is verified on this drive (AccuDisc run B), because §2's slips are its exact
   signature. **`verify_passes >= 2` is not optional**: it is what makes step 4 sound.
   `overlap_sectors` stays for seam slips but is not a substitute — and it is weaker than
   its name suggests. **A seam mismatch currently condemns only the seam sectors**
   (`s < prev_ext_n`, nothing propagates outward), so a chunk that landed 48 bytes late
   yields two `SUSPECT` sectors at the seam and ~20 wrong ones still marked `OK` in the same
   displaced transfer. AccuDisc call this a defect on their side, queued as the first item of
   their ladder work (§199.2). Until it is fixed, **never read `overlap_sectors` as "the
   transfer was position-checked"** — which is why §4.4 keeps `verify_passes >= 2` as the
   condition and overlap as an extra rather than an alternative.
   **`c2_retries` stays 0 even on 0.42.0**, until a measurement shows it adds recovery
   under verify. Measured on hardware (run B, AccuDisc 2026-09-16g): its anchor is the
   chunk *as first delivered*, so when that chunk is itself late the rescue inherits the
   shift and labels the sector `RECOVERED` (raw 113098, +48 bytes with 2 damaged bytes).
   That is the documented limit of a reference that shares the displacement. 0.42.0 costs something too: a flagged sector with no stable neighbour keeps
   its flagged copy, so `c2_retries` may add less here than R3's table suggests.
4. **Accept per sector.** A delivered copy is accepted when **the read that delivered it
   had a position witness, i.e. `verify_passes >= 2`** (a second transfer of the same
   sectors; run B in §2). `overlap_sectors` alone does **not** qualify: it compares chunk
   seams, so it only sees a slip that differs between neighbouring chunks (AccuDisc
   2026-09-16f),
   its map state is `OK` or `RECOVERED`, its own C2 block is all zero, **and** it is not in
   the widened Q-position lane (below). `SUSPECT`, `C2`, `HARD` are never accepted.

   **The fourth condition, added 2026-09-18 (AccuDisc §199.3/§199.3b).** The read asks for
   `Sub.RAW` and a `subq_map`, giving a per-sector `MISPOSITION` flag: the sector's own
   CRC-valid ADR=1 Q frame named an LBA other than the one commanded.
   - *It is an addition, never a swap.* It **cannot** see run B's class — the Q check is a
     whole-sector test, quantum 588 samples, against displacements of 12 and 24 samples, so
     a record 48 bytes late still carries the Q frame naming its own sector. AccuDisc state
     plainly that `subq_misposition` would **not** have fired at raw 113068-71 or 113098.
     `verify_passes >= 2` remains the witness for that class.
   - *What it does catch* is whole-sector re-acquisition: the drive loses lock and returns
     valid audio from elsewhere with a CRC-valid Q naming where it really went. 17/17 caught,
     zero false positives over 1.4M sectors on the PX-716A. C2 is silent for it, repeated
     reads agree with each other, and a seam check looks at seams while it sits mid-transfer.
     Nothing else in the rule sees it.
   - *Gate on the WIDENED set.* Audio and subchannel are offset in time — worst case 2
     sectors on the PX-716A — so the leading edge of a slip carries a **correct** Q frame
     beside already-wrong audio and is not flagged. The seam publishes the measured lane and
     the widened one separately (`q_misposition` / `position_suspect`, margin
     `ADSC_QPOS_MARGIN` = 4, AccuDisc's own constant); the rule reads the second, a report of
     what the drive did quotes the first. Folding them overstates the measurement; gating on
     the narrow one accepts exactly what the lane exists to catch.
   - *Cost is nil on this drive:* AccuDisc measured `--sub raw` and no-sub deliveries of the
     same span byte-identical, so the capture changes no alignment. The first accepted copy wins and the sector leaves the target set; later
   passes re-read only the shrunken spans. *Variant for the bench:* require two accepted
   copies at different speeds to agree byte-for-byte.
5. **Gate.** When the target set is empty, or after each pass, overlay accepted sectors onto
   the track's current PCM in memory and run `match_track_pcm`. Pass → splice the verified
   bytes (same sample-exact write as today) and stop. Fail with an empty target set → the
   acceptance rule accepted a wrong copy: record it and fall through to `track-ladder`.
6. **Budget.** Stop at the profile's `passes` or `budget_s`, whichever first.

**Falsifier:** a track whose target set empties and still fails AR. On the bench the key
then names the accepted-but-wrong sectors, which is what decides between the strict variant
and a different acceptance rule.

## 5. Changes by file

- **`accudisc_reader.py` (the seam).** New `read_span_detail(device, start, count, *,
  read_speed, verify_passes, overlap_sectors, c2_retries, speed_ladder, progress_cb)`
  returning PCM, C2 (294 B/sector), status map and stats (`slips`, `sectors_flagged`,
  speed honoured). Built on `_read_span_binding`'s own-sink pattern; its length and
  `sector_len` guards carry over, with the sector length now 2646. Feature-detect
  `caller_map_buffers`; without it the rung declines (no C2-census fallback: it cannot tell
  HARD from C2).
- **`cdda2img.py`.** `_recover_flagged_spans(...)` beside `_recover_failed_tracks`, called
  after CTDB and before it, on the tracks still failing. Shares the AR responses fetched
  once and the recovery map view.
- **`recovery_profile.py` / `validation.py`.** First real consumer of `granularity` and
  `span`. New `granularity = "span"` value (map-derived spans, distinct from the damage-blind
  `"sector"`), plus `span_gap` / `span_pad`. New shipped profile **`span-flagged`**. The
  experimental `span-fixed` / `sector-hammer` / `sector-runup` stay untouched controls:
  their 1/16, 2/20, 2/20 were fixed-size damage-blind spans and are **not** evidence about
  this rung.
- **`validation.py` `PROFILE_SCHEMA` — three changes, all in the `span-flagged` commit.**
  Read 2026-09-18; the schema is narrower than §4 assumed.
  1. **`c2_retries` and `verify_passes` do not exist as profile fields.** There is a
     `verify` *bool* and no retry count at all — `--ad-c2-retries` reaches only PROV
     `recovery_ad_flags`. §4.3/§4.4 need both as real fields, so they are added here.
  2. **Adding them creates the obligation to mirror AccuDisc 0.43.0's constraint.** That
     engine returns `ERR_INVAL` for `c2_retries > 0` without `verify_passes >= 2`. Without a
     sanity rule a hand-edited profile fails **mid-rip, from the engine**, after the disc has
     been read; with one it fails at config load. This is exactly the `--retries 256` class
     `validation.py` was built for — well-formed and still illegal — and it keeps our copy of
     the constraint pinned to theirs rather than to a comment.
  3. **The existing coherence rules must widen, not just gain a sibling.** `span > 0
     requires granularity="sector"` and the same rule for `run_up` predate this rung; adding
     `granularity="span"` without widening them makes `span_gap`/`span_pad` unusable on the
     very granularity they exist for. Widen to `in {"sector", "span"}` and keep a negative
     control that still rejects `track` / `whole-disc`.

- **PROV** (rbi_spec §6.3.1 first, spec-before-code): `recovery_track_<n>=span_matched@p<k>`
  alongside the existing `matched@<N>X` / `unrecovered`; `span_targets_track_<n>=<sectors>/
  <spans>`; `span_unresolved_track_<n>=<sectors>` when the rung gave up;
  `span_accepted_unverified_track_<n>` for the §4.5 falsifier. Feeds the open PROV key audit.
- **Docs:** cdda2img `docs/reference/RECOVERY.md`, the man page, CLAUDE.md rip pipeline.
- **Tests + `tools/check_test_count.py` FLOOR** in the same commit.

## 6. Test plan

**Offline (no drive, now):**
- Clustering on the capture maps; span count and sector totals vs whole-track cost
  (track 9: ~435 of 14,343 sectors per pass, before shrinkage).
- The acceptance rule against the locator captures and a fake binding that replays them,
  including a synthetic +96-byte slip marked `RECOVERED` with clean C2 (must not be accepted
  once the engine marks it `SUSPECT`; must be caught by the gate when it is not).
- Commit-on-pass: a candidate that fails AR leaves the PCM file byte-identical.
- Controls with teeth: remove the C2-clean condition, or write before the gate, and the
  tests must fail.

**On the LITE-ON, under the protocol (claim, ack, `flock /var/tmp/sr0.lock` for the whole
run, `/var/tmp/sr0.owner` = who/what/ETA, release):**
- *H0 (AccuDisc's):* their 0.41/0.42 verification read of the same spans. Ours waits for it.
- *H1:* a bench tool (`tools/span_recovery_bench.py`) that runs §4 steps 1–5 on Tracy's
  tracks 8, 9, 11 only, and logs every pass scored against the key: sectors accepted,
  accepted-but-wrong, time. Both acceptance variants. Quiet AccuDisc build tree, engine
  version named in the claim.
- *H2:* a full `cdda2img rip` of Tracy with `--profile span-flagged`, expected 11/11.

## 7. Decisions — settled by Keith, 2026-09-18

1. **Placement: before `track-ladder`.** The rung runs after CTDB and before
   `_recover_failed_tracks`, which stays as the fallback. A track whose target set empties
   and still fails AR falls through to the ladder unchanged (§4.5).
2. **Default: opt-in.** `span-flagged` is reachable only via `--profile` until H2 verifies
   11/11 on Tracy. `track-ladder` remains `resolve_recovery`'s built-in default (its
   measured 19/20 is not displaced by an offline score). Promoting it is a separate commit
   that cites the H2 run.
3. **Keep the locator: persist it, as a bit-packed block — but AFTER the bench, not before.**
   A new optional block carries the capture-pass damage lane at **1 bit per sector**,
   `BLOCK_FLAG_SKIP` set. Measured 2026-09-18 on both Tracy containers (162,892 sectors):
   **20,362 B**, 0.0053 % of a 383 MB container; the 80-minute ceiling is 45,000 B. The full
   map byte (state + severity nibbles) was rejected at 162,892 B — 8× the cost for two
   nibbles no consumer reads, where the 0/1 lane is exactly what `disc_damage` already hands
   recovery.
   - **It is not on this rung's critical path, and must not be sequenced as if it were.**
     §4.1 takes its targets from `disc_damage`, captured on every rip and passed to recovery
     **in-process**. The block buys one thing the rung does not need: re-running recovery
     against an *existing* container without re-reading the disc. H1 and H2 do not consume
     it. Order is therefore **rung → bench → block**, and spec-before-code binds the block
     change, not the rung.
   - **It IS a minor version bump: v6.1.** The earlier draft said "no format bump, the same
     reasoning §6.5 used to make ARIP omissible" — that was wrong, and the right precedent is
     **v4.1**, which added the optional ART block as an explicit backwards-compatible minor
     bump. §6.5 governs *omitting* a block the spec already defines; this *defines* one.
   - **Non-breaking is verified, not assumed**, and rests on two independent gates. Rule 3
     (`container.py:1408`): a reader **warns and proceeds** when `version_minor` exceeds what
     it knows. Rule 32: an unknown `type_id` **with** `BLOCK_FLAG_SKIP` is accepted, without
     it the file is rejected — exercised today by
     `tests/test_format_gates.py::test_unknown_block_type_is_accepted_only_when_skippable`,
     which passes. So a v6.0 reader clears both and ignores the block. Registering an id
     ahead of its layout is also established here (`CTDB`, §6.7 — registered, layout undefined).

**Still open, not gating this rung:** whether an exit-4 rip should be catalogued
(`register_rbi` runs before the checks), and whether AccuDisc should force a position
witness on plain reads.
