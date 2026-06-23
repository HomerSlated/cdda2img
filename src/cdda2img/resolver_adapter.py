"""
resolver_adapter.py — B4 collect->resolve adapter (the wired sole committer).

Bridges the lookup result types (``DiscMeta`` / ``RBIDisc``) to
:class:`~cdda2img.field_resolver.FieldProposal`, so the collect->resolve resolver
in ``field_resolver.py`` reproduces ``mb_lookup._merge_into_disc`` with no
behaviour change *except two documented divergences* (see below). This is the
strangler keystone: the equivalence test

    disc_from_resolution(
        resolve(baseline_proposals(disc) + meta_to_proposals(meta, src)), disc
    ) == _merge_into_disc(meta, disc)

is what makes every later wiring stage safe to land.

The trust model is the *reproduce-today* ladder (see :data:`_REPRODUCE_META_TRUST`):

- the disc accumulator (:attr:`Source.BASELINE`) proposes at :data:`_BASELINE_TRUST`
  (above every network source), so it wins fill-blank exactly as
  ``_merge_into_disc``'s disc-priority does;
- network sources take DISTINCT descending levels in today's call order (MB >
  Discogs > stage-7 > CDDB), so a *multi-source* merge reproduces first-writer-wins
  order-independently (B-3 — proven by ``test_merge_sequence``);
- the §10 canonical MCN sits ABOVE baseline (it overwrites ``disc.catalog``);
- the ``"Unknown Artist"`` sentinel sits BELOW every real source (it loses to any
  real value but survives alone).

This reproduces today's behaviour except for B-6 (LANDED 2026-06-23), the *only*
intentional ranking change in the whole refactor: a single per-(source, field)
override making Discogs outrank MB for the catalogue pair ``catalog_number`` /
``label`` (see :data:`_FIELD_TRUST_OVERRIDE`; ``country`` is excluded — it couples
with the §10 ``preferred_country`` selection). The old B-6 "baseline-overwrite" rule
was dropped (its CD-Text-is-poor premise was overturned). Do not add further ranking
changes here without an explicit decision.

Equivalence DOMAIN (advisor, 2026-06-22): equivalence holds on the *live*
post-``strip_pressing_mbid`` domain, where recording-level metas (ISRC / AcoustID /
stage-7 duration) already carry ``mb_release_id=None``. Off that domain the
resolver is intentionally STRICTER than the merge: a recording-level source with a
*non-empty* ``mb_release_id`` raises at :class:`FieldProposal` construction (C2)
where the merge would silently leak it. That strictness is the C2 win, not a
divergence to paper over — hence :func:`meta_to_proposals` skips empties *before*
constructing (an empty ``mb_release_id`` must never reach the raising constructor).

Former divergences from ``_merge_into_disc`` (advisor 2026-06-22) — BOTH RESOLVED
2026-06-23, clearing the B-4 flip gate. Root cause was that the merge *rebuilds* a
meta-matched track entry (so a disc-side value that validates-to-empty gets nulled),
whereas the resolver *patches* only proposed fields and keeps the base otherwise:

1. **Invalid disc ISRC.** RESOLVED by **uniform drop** (:func:`sanitize_base`, the
   user's 2026-06-23 decision): an invalid on-disc ISRC is dropped on **every**
   track before assembly, aligning with the R13 validation infrastructure. ``_merge``
   scrubs an invalid ISRC only on MB-*matched* tracks (it rebuilds those and
   re-validates) and keeps the garbage verbatim on unmatched ones — a match-dependent
   quirk the collect->resolve adapter deliberately cannot see. So the resolver now
   AGREES with ``_merge`` on the matched case (both -> ``None``) and *intentionally
   diverges* on the unmatched case (resolver -> ``None``, merge -> garbage). The
   chosen divergence is pinned by
   ``test_invalid_isrc_dropped_uniformly_unmatched_DIVERGES_from_merge``.
2. **Duplicate track numbers in ``meta.tracks``.** RESOLVED by **reproduce-today**:
   :func:`meta_to_proposals` collapses ``meta.tracks`` to last-per-number (matching
   ``_merge``'s last-wins ``meta_by_num`` dict) before emitting, so the resolver no
   longer emits equal-trust duplicates that ``resolve``'s ``max`` would break
   first-wins. Behaviour-neutral. Low-reachability (malformed meta).

Out-of-domain edge (documented, not special-cased): the *falsy-but-present* class.
The resolver canonicalises an absent disc-level value to ``None`` (it skips empty
proposals, leaving the base); ``_merge``'s ``or``-chains instead collapse a
falsy-but-present value (``""`` for strings, ``0`` for ints) inconsistently —
yielding ``None``, ``""`` or the other operand depending on which side carries it
and whether the field has a trailing ``or None`` guard. So a disc-level merged
optional whose value is ``""``/``0`` (rather than ``None``) can resolve to a
different *representation* of "absent" than ``_merge`` produces. This never arises
live: ``RBIDisc`` defaults every optional merged field to ``None`` (never ``""``),
and ``album``/``artist`` are required ``str`` whose special merge formula falls
back to the disc value, so it agrees. A Discogs release id is never ``0``. Hence
documented, not special-cased; the resolver's canonical ``None`` is arguably
cleaner. The property test in ``test_resolver_adapter.py`` models this live
invariant (optional fields None-or-nonempty) and is exact within it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from cdda2img.field_resolver import (
    Field,
    FieldProposal,
    Source,
    Trust,
    disc_from_resolution,
    resolve,
)
from cdda2img.lookup_result import DiscMeta
from cdda2img.rbi_format import RBIDisc
from cdda2img.validators import validate_isrc

# Reproduce-today per-source trust map. B-6 added exactly one per-(source, field)
# override on top of this (catalogue: Discogs > MB — see _FIELD_TRUST_OVERRIDE); all
# other (source, field) pairs use these defaults. The ordering is load-bearing:
#
# - baseline outranks every *network* source -> disc-priority fill-blank is exact;
# - network sources take DISTINCT, descending levels in today's call order
#   (MB > Discogs > stage-7 > CDDB), so a multi-source merge reproduces today's
#   first-writer-wins **order-independently** (no equal-trust first-seen tiebreak);
# - the §10 canonical MCN (CANONICAL_MCN) sits ABOVE baseline, because phase A of
#   `_prepopulate_from_discogs` *overwrites* `disc.catalog` with it unconditionally
#   (it is not fill-blank). That is why baseline is OBJECTIVE, not MANUAL: lowering
#   it makes room above for the canonical MCN, and frees MANUAL for genuine user
#   menu input (B-5).
#
# AcoustID is deliberately absent: `_r6_tally_and_merge` merges no fields (it is
# corroboration-only), so it contributes no proposals. That ACOUSTID's semantic
# enum level (60) would sit out of call order (Discogs runs first, at 55) is
# therefore moot — load-bearing, and asserted by test.
_BASELINE_TRUST = Trust.OBJECTIVE  # disc accumulator wins fill-blank over network

_REPRODUCE_META_TRUST: dict[Source, Trust] = {
    Source.MB_DISC_ID: Trust.DISC_ID,
    Source.DISCOGS: Trust.DISCOGS,
    Source.ACOUSTID: Trust.ACOUSTID,
    Source.ISRC: Trust.ISRC,
    Source.DURATION: Trust.DURATION,  # stage-7
    Source.CDDB: Trust.CDDB,
    Source.CANONICAL_MCN: Trust.MANUAL,  # §10 verdict overrides baseline catalog
    # B-5: a menu "update" apply fills blanks only, so MENU sits BELOW the
    # OBJECTIVE baseline (disc-priority, == _merge_into_disc). A menu "overwrite"
    # apply passes trust_override=MANUAL instead (see apply_menu_selection), so this
    # level is only ever used for update. DISC_ID is a high network level that still
    # yields to the existing disc — the user picked the source but chose not to
    # overwrite. Within one apply only baseline vs this meta compete, so the exact
    # level below OBJECTIVE does not affect the result.
    Source.MENU: Trust.DISC_ID,
}

# B-6 (2026-06-23) — the single intentional ranking change of the whole B4 arc, and
# the ONLY per-(source, field) override. Discogs is the catalogue authority, so it
# outranks MB for the catalogue PAIR (catalog_number / label): per the §7.1 2-D table,
# Discogs=80 > MB=60 for these fields (both still below the OBJECTIVE=100 baseline, so
# a present on-disc catalogue value wins fill-blank). This fixes the live D5 inversion
# that RBI v6.0 created by persisting these fields — MB had won only by merge
# call-order accident. Pipeline-independent; every other (source, field) falls through
# to the reproduce-today map above.
#
# `country` is DELIBERATELY EXCLUDED (user decision 2026-06-23). Unlike catalog_number
# / label, it couples across layers: §10 Layer-1 release selection may pick the MB
# pressing *because* its country matched the user's `preferred_country` config, so
# letting Discogs override the committed country at Layer-2 would undo that preference.
# country therefore stays reproduce-today (MB > Discogs), honouring the §10 selection.
#
# The old "B2" baseline-overwrite rule (disc-ID MB overwrites a present CD-Text
# album/artist/title on rip) was DROPPED by user decision: its "CD-Text is poor"
# premise was overturned — CD-Text-when-present is reliable; the online DBs are the
# error-prone source. So the CD-Text/disc-baked baseline is NOT lowered here.
_CATALOGUE_FIELDS: frozenset[Field] = frozenset({
    Field.CATALOG_NUMBER,
    Field.LABEL,
})
_FIELD_TRUST_OVERRIDE: dict[tuple[Source, Field], Trust] = {
    **{(Source.DISCOGS, f): Trust.DISC_ID for f in _CATALOGUE_FIELDS},  # 80 — wins
    **{
        (Source.MB_DISC_ID, f): Trust.ACOUSTID for f in _CATALOGUE_FIELDS
    },  # 60 — yields
}

# The "Unknown Artist" sentinel is NOT empty — _merge_into_disc treats it as absent
# on the *accumulator* side at every step, so any real artist/performer from any
# source overrides it, but it remains the final value if nothing else supplies one.
# That is precisely a value at a trust BELOW every real source (min real = CDDB=20):
# it loses every contest against a real value yet survives alone. Modelling it this
# way (rather than skipping it) is required once a *meta* can carry the sentinel —
# see test_merge_sequence's property test, which caught the skip-based shortcut.
_SENTINEL_TRUST = Trust.BASELINE  # 10 — below CDDB (20); the lowest real source

_UNKNOWN_ARTIST = "Unknown Artist"  # sentinel treated as absent (matches _merge)

# Skip reasons (§11.5 traceability). The "Unknown Artist" sentinel is NOT a skip
# reason — it is emitted as a low-trust proposal (see _emit_named / _SENTINEL_TRUST).
_R_EMPTY = "empty"
_R_INVALID_ISRC = "invalid-isrc"
_R_ABSTAIN = "abstain-meta-priority"


def trust_for(source: Source, field: Field, pipeline: str) -> Trust:
    """Trust for a ``(source, field, pipeline)``.

    Baseline at :data:`_BASELINE_TRUST`; network sources at distinct descending
    levels in today's call order (see :data:`_REPRODUCE_META_TRUST`). The **one**
    per-field override is B-6's catalogue correction (:data:`_FIELD_TRUST_OVERRIDE`):
    Discogs outranks MB for ``catalog_number`` / ``label`` / ``country``. Everything
    else is the reproduce-today map.

    *pipeline* is unused: B-6 dropped the per-pipeline baseline-overwrite rule (its
    "CD-Text is poor" premise was overturned), so the CD-Text/disc-baked baseline is
    not lowered. The parameter stays in the signature as a seam for any future
    pipeline-specific tuning, so call sites do not change.
    """
    del pipeline  # reserved seam; no per-pipeline rule today (B-2 was dropped)
    override = _FIELD_TRUST_OVERRIDE.get((source, field))
    if override is not None:
        return override
    if source is Source.BASELINE:
        return _BASELINE_TRUST
    return _REPRODUCE_META_TRUST[source]


def sanitize_base(disc: RBIDisc) -> RBIDisc:
    """Return *disc* with invalid on-disc track ISRCs (R13/ISO-3901) nulled.

    The base the resolver assembles onto must not smuggle a malformed ISRC into
    the output via a track that no proposal overwrote. Per the 2026-06-23 decision
    (B-1 divergence 1, resolved), invalid ISRCs are dropped **uniformly** — on
    every track — a deliberate, documented divergence from ``_merge_into_disc``,
    which scrubs them only on MB-matched tracks and keeps the garbage verbatim on
    unmatched ones (mb_lookup.py:758-782). Dropping uniformly aligns with the R13
    validation infrastructure: an invalid ISRC is garbage, not a guess.

    Valid ISRCs are left as-is here — :func:`baseline_proposals` proposes their
    normalised form, which wins at assembly, so the committed value is canonical
    either way. This must wrap the base at **every committed-disc assembly**
    (production shadow build + the equivalence test paths) so they agree; it is a
    no-op on a disc with no malformed ISRCs. (It is *not* applied to bare unit tests
    of the ``disc_from_resolution`` primitive itself — those exercise the assembler,
    not the committed-disc contract.)

    NB a related, pre-existing **representational** divergence rides the same axis
    (advisor 2026-06-23): because :func:`baseline_proposals` normalises *every* valid
    on-disc ISRC, an UNMATCHED track with a valid but non-canonical (hyphenated /
    lowercase) ISRC resolves to the canonical form, where ``_merge_into_disc`` (which
    normalises only matched/rebuilt tracks) keeps the raw value. Resolver -> canonical,
    merge -> raw; normalising is arguably cleaner (the RBI/TOC ISRC field is 12 chars,
    no hyphens). Same bucket as the falsy-but-present -> ``None`` canonicalisation
    below; pinned by
    ``test_valid_noncanonical_isrc_normalised_uniformly_unmatched_DIVERGES_from_merge``.
    """
    new_tracks = [
        replace(t, isrc=None) if (t.isrc and validate_isrc(t.isrc) is None) else t
        for t in disc.tracks
    ]
    return replace(disc, tracks=new_tracks)


def canonical_mcn_proposal(chosen: str | None) -> list[FieldProposal]:
    """The §10 canonical-MCN verdict as a single high-trust CATALOG proposal.

    Reproduces phase A of ``_prepopulate_from_discogs`` (``disc.catalog = chosen``,
    an *unconditional* overwrite when a canonical MCN is picked). At
    :data:`Trust.MANUAL` it outranks the baseline catalog and every network source,
    so it wins exactly as the in-place overwrite does. Returns ``[]`` when no MCN
    was chosen (no candidates), leaving catalog to baseline/meta fill-blank.
    """
    if not chosen:
        return []
    return [
        FieldProposal(
            field=Field.CATALOG,
            value=chosen,
            trust=_REPRODUCE_META_TRUST[Source.CANONICAL_MCN],
            source=Source.CANONICAL_MCN,
        )
    ]


@dataclass(frozen=True)
class Skip:
    """A value the adapter declined to propose, with the reason (§11.5).

    Collected only when a caller passes a ``skips`` list; the equivalence path
    ignores it. This is the other half of the traceability story from
    ``Resolution.contenders``: contenders shows what competed, ``Skip`` shows what
    never got to compete (and why) — so a silently-dropped correct value becomes
    visible rather than invisible.
    """

    field: Field
    track_number: int | None
    value: object
    reason: str


def _emit(
    out: list[FieldProposal],
    skips: list[Skip] | None,
    field: Field,
    value: object,
    trust: Trust,
    source: Source,
    track_number: int | None = None,
) -> None:
    """Append a proposal for *value*, or record an ``empty`` skip if it carries no
    information. Empties are dropped BEFORE construction so a recording-level
    source can never reach the C2-raising :class:`FieldProposal` constructor with
    an empty ``mb_release_id`` (advisor, 2026-06-22)."""
    if value is None or value == "":
        if skips is not None:
            skips.append(Skip(field, track_number, value, _R_EMPTY))
        return
    out.append(
        FieldProposal(
            field=field,
            value=value,
            trust=trust,
            source=source,
            track_number=track_number,
        )
    )


def _emit_named(
    out: list[FieldProposal],
    skips: list[Skip] | None,
    field: Field,
    value: object,
    normal_trust: Trust,
    source: Source,
    track_number: int | None = None,
) -> None:
    """Emit an artist/performer value. The ``"Unknown Artist"`` sentinel goes in at
    :data:`_SENTINEL_TRUST` (below every real source) so any real value outranks it
    regardless of source order, yet it survives as the final value if nothing else
    proposes one — reproducing ``_merge``'s disc-side ``!= _unknown`` check applied
    at every merge step. Empty/None still skips via :func:`_emit`."""
    trust = _SENTINEL_TRUST if value == _UNKNOWN_ARTIST else normal_trust
    _emit(out, skips, field, value, trust, source, track_number)


def baseline_proposals(
    disc: RBIDisc, pipeline: str = "", *, skips: list[Skip] | None = None
) -> list[FieldProposal]:
    """Proposals from the disc accumulator (:attr:`Source.BASELINE`) at max trust,
    so it wins fill-blank exactly as ``_merge_into_disc``'s disc-priority does.

    Reproduces three ``_merge`` quirks:

    - the ``"Unknown Artist"`` sentinel (disc artist + track performer) is emitted at
      :data:`_SENTINEL_TRUST` so any real value outranks it but it survives alone
      (see :func:`_emit_named`);
    - per-track ISRC is validated (R13) and the *normalised* value is proposed; an
      invalid disc-side ISRC is skipped so the (MB-ingress-validated) meta ISRC
      wins — reproducing ``entry_isrc or mt.isrc``;
    - ``disc_number`` / ``disc_total`` are NOT proposed. They are meta-priority in
      the merge; abstaining lets the sole meta proposer win, and ``replace``
      preserves the base value when meta is absent.
    """
    src = Source.BASELINE
    out: list[FieldProposal] = []

    def t(field: Field) -> Trust:
        return trust_for(src, field, pipeline)

    # --- disc-level, plain fill-blank ---
    _emit(out, skips, Field.ALBUM, disc.album, t(Field.ALBUM), src)
    # artist: "Unknown Artist" sentinel goes in at _SENTINEL_TRUST (see _emit_named)
    _emit_named(out, skips, Field.ARTIST, disc.artist, t(Field.ARTIST), src)
    _emit(out, skips, Field.CATALOG, disc.catalog, t(Field.CATALOG), src)
    # disc_number / disc_total: meta-priority quirk -> baseline abstains
    if skips is not None:
        skips.append(Skip(Field.DISC_NUMBER, None, disc.disc_number, _R_ABSTAIN))
        skips.append(Skip(Field.DISC_TOTAL, None, disc.disc_total, _R_ABSTAIN))
    _emit(out, skips, Field.RELEASE_DATE, disc.release_date, t(Field.RELEASE_DATE), src)
    _emit(
        out,
        skips,
        Field.CATALOG_NUMBER,
        disc.catalog_number,
        t(Field.CATALOG_NUMBER),
        src,
    )
    _emit(out, skips, Field.LABEL, disc.label, t(Field.LABEL), src)
    _emit(out, skips, Field.COUNTRY, disc.country, t(Field.COUNTRY), src)
    _emit(
        out,
        skips,
        Field.ORIGINAL_RELEASE_DATE,
        disc.original_release_date,
        t(Field.ORIGINAL_RELEASE_DATE),
        src,
    )
    _emit(
        out, skips, Field.MB_RELEASE_ID, disc.mb_release_id, t(Field.MB_RELEASE_ID), src
    )
    _emit(
        out,
        skips,
        Field.MB_RELEASE_GROUP_ID,
        disc.mb_release_group_id,
        t(Field.MB_RELEASE_GROUP_ID),
        src,
    )
    _emit(
        out,
        skips,
        Field.DISCOGS_RELEASE_ID,
        disc.discogs_release_id,
        t(Field.DISCOGS_RELEASE_ID),
        src,
    )
    _emit(out, skips, Field.SET_TITLE, disc.set_title, t(Field.SET_TITLE), src)

    # --- track-level ---
    for entry in disc.tracks:
        n = entry.track_number
        _emit(out, skips, Field.TRACK_TITLE, entry.title, t(Field.TRACK_TITLE), src, n)
        _emit_named(
            out,
            skips,
            Field.TRACK_PERFORMER,
            entry.performer,
            t(Field.TRACK_PERFORMER),
            src,
            n,
        )
        # ISRC: validate (R13); propose the normalised value. An invalid disc-side
        # ISRC is dropped so the meta ISRC fills (reproduces entry_isrc or mt.isrc).
        normalised = validate_isrc(entry.isrc) if entry.isrc else None
        if entry.isrc and normalised is None:
            if skips is not None:
                skips.append(Skip(Field.TRACK_ISRC, n, entry.isrc, _R_INVALID_ISRC))
        else:
            _emit(out, skips, Field.TRACK_ISRC, normalised, t(Field.TRACK_ISRC), src, n)

    return out


def meta_to_proposals(
    meta: DiscMeta,
    source: Source,
    pipeline: str = "",
    *,
    skips: list[Skip] | None = None,
    trust_override: Trust | None = None,
) -> list[FieldProposal]:
    """Proposals from a network meta *source* at :data:`_META_TRUST`, so it only
    fills the blanks the baseline left.

    Empty values are skipped before construction (see :func:`_emit`): on the live
    post-``strip_pressing_mbid`` domain a recording-level meta has
    ``mb_release_id=None`` and is skipped here; a *non-empty* ``mb_release_id``
    from a recording-level source WILL raise at construction — the intended C2
    strictness. The meta ISRC is proposed verbatim (it was validated at MB
    ingress; only the disc-side ISRC is re-validated, in
    :func:`baseline_proposals`).

    *trust_override* (B-5): when set, every field is proposed at this trust instead
    of the per-source reproduce-today map — used by :func:`apply_menu_selection` to
    emit a user-endorsed "overwrite" selection at :data:`Trust.MANUAL`. The
    ``"Unknown Artist"`` sentinel is *still* demoted to :data:`_SENTINEL_TRUST`
    (via :func:`_emit_named`) even under an override — a real value must never lose
    to the sentinel, whatever the user chose (a documented divergence from
    ``_overwrite_disc``, which takes the sentinel via ``meta.artist or disc.artist``).
    """
    out: list[FieldProposal] = []

    def t(field: Field) -> Trust:
        if trust_override is not None:
            return trust_override
        return trust_for(source, field, pipeline)

    _emit(out, skips, Field.ALBUM, meta.album, t(Field.ALBUM), source)
    _emit_named(out, skips, Field.ARTIST, meta.artist, t(Field.ARTIST), source)
    _emit(out, skips, Field.CATALOG, meta.catalog, t(Field.CATALOG), source)
    _emit(out, skips, Field.DISC_NUMBER, meta.disc_number, t(Field.DISC_NUMBER), source)
    _emit(out, skips, Field.DISC_TOTAL, meta.disc_total, t(Field.DISC_TOTAL), source)
    _emit(
        out, skips, Field.RELEASE_DATE, meta.release_date, t(Field.RELEASE_DATE), source
    )
    _emit(
        out,
        skips,
        Field.CATALOG_NUMBER,
        meta.catalog_number,
        t(Field.CATALOG_NUMBER),
        source,
    )
    _emit(out, skips, Field.LABEL, meta.label, t(Field.LABEL), source)
    _emit(out, skips, Field.COUNTRY, meta.country, t(Field.COUNTRY), source)
    _emit(
        out,
        skips,
        Field.ORIGINAL_RELEASE_DATE,
        meta.original_release_date,
        t(Field.ORIGINAL_RELEASE_DATE),
        source,
    )
    _emit(
        out,
        skips,
        Field.MB_RELEASE_ID,
        meta.mb_release_id,
        t(Field.MB_RELEASE_ID),
        source,
    )
    _emit(
        out,
        skips,
        Field.MB_RELEASE_GROUP_ID,
        meta.mb_release_group_id,
        t(Field.MB_RELEASE_GROUP_ID),
        source,
    )
    _emit(
        out,
        skips,
        Field.DISCOGS_RELEASE_ID,
        meta.discogs_release_id,
        t(Field.DISCOGS_RELEASE_ID),
        source,
    )
    _emit(out, skips, Field.SET_TITLE, meta.set_title, t(Field.SET_TITLE), source)

    # Duplicate track numbers in malformed meta: ``_merge_into_disc`` keys tracks
    # by number in a last-wins dict (``mb_lookup.py:757``
    # ``{t.number: t for t in meta.tracks ...}``), so collapse to last-per-number
    # before emitting. Emitting both at equal trust would resolve first-wins
    # (``resolve``'s ``max``) and diverge from the merge — this was B-1 divergence 2,
    # resolved here 2026-06-23 (reproduce-today, behaviour-neutral: the resolver is
    # not yet the committer). Dict-comprehension insertion order keeps each number's
    # first appearance as the iteration position with the last-seen value as content,
    # matching ``meta_by_num`` exactly.
    meta_by_num = {mt.number: mt for mt in meta.tracks if mt.number is not None}
    for n, mt in meta_by_num.items():
        _emit(out, skips, Field.TRACK_TITLE, mt.title, t(Field.TRACK_TITLE), source, n)
        _emit_named(
            out,
            skips,
            Field.TRACK_PERFORMER,
            mt.performer,
            t(Field.TRACK_PERFORMER),
            source,
            n,
        )
        _emit(out, skips, Field.TRACK_ISRC, mt.isrc, t(Field.TRACK_ISRC), source, n)

    return out


def apply_menu_selection(
    disc: RBIDisc, selected: DiscMeta, *, overwrite: bool
) -> RBIDisc:
    """Apply a user-selected search result onto *disc* via the trust resolver (B-5).

    The single menu source-merge path: replaces ``_merge_into_disc`` (update) and
    ``_overwrite_disc`` (overwrite) at the three ``ResultsScreen`` apply sites
    (MB / Discogs / AcoustID). *disc* is the live ``ctl.disc`` baseline — the
    resolver re-baselines on it for each apply, so a *sequence* of menu applies
    composes exactly as the in-place merges did (later wins, earlier fills), with no
    persistent accumulator (that is B-7).

    - **overwrite** — the user endorses *selected* as authoritative, so it is emitted
      at :data:`Trust.MANUAL` (above the OBJECTIVE baseline): it wins, the baseline
      fills its blanks. Reproduces ``_overwrite_disc`` (meta-priority).
    - **update** — *selected* is emitted at the :attr:`Source.MENU` map trust (below
      the baseline): it only fills blanks. Reproduces ``_merge_into_disc``
      (disc-priority) — this *is* the B-1 keystone with ``disc = ctl.disc``.

    Accepted divergences (resolver-cleaner; pinned in ``test_menu_apply_resolver``):
    a *selected* ``artist``/``performer`` of ``"Unknown Artist"`` is demoted below a
    real baseline value (``_overwrite_disc`` would take the sentinel); and the three
    ISRC carve-outs ride :func:`sanitize_base` + baseline normalisation as elsewhere.
    These do not arise from pipeline-supplied data: on the rip/import path *disc* is
    the B-4 committed disc (already ISRC-sanitised), and on the create path
    ``build_toc_entries`` sets no per-track ISRC. Where reachable at all (e.g. a manual
    per-track ISRC edit), the divergence *is* the approved drop/normalise behaviour.
    """
    trust_override = Trust.MANUAL if overwrite else None
    proposals = baseline_proposals(disc) + meta_to_proposals(
        selected, Source.MENU, trust_override=trust_override
    )
    return disc_from_resolution(resolve(proposals), sanitize_base(disc))
