"""
resolver_adapter.py — B4 B-1: the "reproduce today" adapter (still UNWIRED).

Bridges the lookup result types (``DiscMeta`` / ``RBIDisc``) to
:class:`~cdda2img.field_resolver.FieldProposal`, so the collect->resolve resolver
in ``field_resolver.py`` can reproduce ``mb_lookup._merge_into_disc`` *exactly*,
with no behaviour change. This is the strangler keystone: the equivalence test

    disc_from_resolution(
        resolve(baseline_proposals(disc) + meta_to_proposals(meta, src)), disc
    ) == _merge_into_disc(meta, disc)

is what makes every later wiring stage safe to land.

The trust model here is deliberately FLAT — two tiers only:

- the disc accumulator (:attr:`Source.BASELINE`) proposes at :data:`_BASELINE_TRUST`
  (max), so it wins fill-blank exactly as ``_merge_into_disc``'s disc-priority does;
- any network meta proposes at :data:`_META_TRUST` (lower), so it only fills the
  blanks the baseline left.

That reproduces today's call-order precedence. The real per-``(source, field,
pipeline)`` ranking — the *only* behaviour change in the whole refactor — is B-6,
and it replaces :func:`trust_for`. Do not enrich it here.

Equivalence DOMAIN (advisor, 2026-06-22): equivalence holds on the *live*
post-``strip_pressing_mbid`` domain, where recording-level metas (ISRC / AcoustID /
stage-7 duration) already carry ``mb_release_id=None``. Off that domain the
resolver is intentionally STRICTER than the merge: a recording-level source with a
*non-empty* ``mb_release_id`` raises at :class:`FieldProposal` construction (C2)
where the merge would silently leak it. That strictness is the C2 win, not a
divergence to paper over — hence :func:`meta_to_proposals` skips empties *before*
constructing (an empty ``mb_release_id`` must never reach the raising constructor).

Known out-of-domain edge: ``discogs_release_id == 0``. ``_merge`` uses ``or``
truthiness (``0`` is falsy -> dropped); the resolver's ``_is_empty`` treats ``0``
as a real value. A Discogs release id is never ``0``, so this is documented here,
not special-cased.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdda2img.field_resolver import Field, FieldProposal, Source, Trust
from cdda2img.lookup_result import DiscMeta
from cdda2img.rbi_format import RBIDisc
from cdda2img.validators import validate_isrc

# Two-tier reproduce map (see module docstring). Placeholder levels until B-6
# installs the real per-(source, field, pipeline) ranking. Only their order
# matters here: baseline must STRICTLY outrank meta so disc-priority is exact and
# resolution stays order-independent (no equal-trust first-seen tiebreak).
_BASELINE_TRUST = Trust.MANUAL  # disc accumulator wins fill-blank
_META_TRUST = Trust.CDDB  # network meta only fills blanks

_UNKNOWN_ARTIST = "Unknown Artist"  # sentinel treated as absent (matches _merge)

# Skip reasons (§11.5 traceability).
_R_EMPTY = "empty"
_R_SENTINEL = "unknown-artist-sentinel"
_R_INVALID_ISRC = "invalid-isrc"
_R_ABSTAIN = "abstain-meta-priority"


def trust_for(source: Source, field: Field, pipeline: str) -> Trust:
    """Trust for a ``(source, field, pipeline)``. B-1: flat two-tier reproduce map.

    *field* and *pipeline* are unused today; they are the seams B-6 will use to
    install the real per-source ranking (create=mutagen-high, rip=CD-Text-low,
    objective ISRC/MCN above network, ...). They stay in the signature so the
    call sites do not change when that lands.
    """
    del field, pipeline  # B-6 seam
    return _BASELINE_TRUST if source is Source.BASELINE else _META_TRUST


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


def baseline_proposals(
    disc: RBIDisc, pipeline: str = "", *, skips: list[Skip] | None = None
) -> list[FieldProposal]:
    """Proposals from the disc accumulator (:attr:`Source.BASELINE`) at max trust,
    so it wins fill-blank exactly as ``_merge_into_disc``'s disc-priority does.

    Reproduces three ``_merge`` quirks:

    - the ``"Unknown Artist"`` sentinel (disc artist + track performer) is treated
      as absent, so meta fills it;
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
    # artist: sentinel treated as absent
    if disc.artist == _UNKNOWN_ARTIST:
        if skips is not None:
            skips.append(Skip(Field.ARTIST, None, disc.artist, _R_SENTINEL))
    else:
        _emit(out, skips, Field.ARTIST, disc.artist, t(Field.ARTIST), src)
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
        if entry.performer == _UNKNOWN_ARTIST:
            if skips is not None:
                skips.append(
                    Skip(Field.TRACK_PERFORMER, n, entry.performer, _R_SENTINEL)
                )
        else:
            _emit(
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
    """
    out: list[FieldProposal] = []

    def t(field: Field) -> Trust:
        return trust_for(source, field, pipeline)

    _emit(out, skips, Field.ALBUM, meta.album, t(Field.ALBUM), source)
    _emit(out, skips, Field.ARTIST, meta.artist, t(Field.ARTIST), source)
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

    for mt in meta.tracks:
        if mt.number is None:
            continue
        n = mt.number
        _emit(out, skips, Field.TRACK_TITLE, mt.title, t(Field.TRACK_TITLE), source, n)
        _emit(
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
