"""
field_resolver.py — B4 collect->resolve metadata trust resolver (Phase A).

**NOT YET WIRED INTO THE PIPELINE.** This module is pure scaffolding: it defines
the trust model and the resolve/assemble primitives, with unit tests, but nothing
calls it yet. Wiring (the ``meta_to_proposals`` adapter + replacing the
``_merge_into_disc`` fold) is Phase B. Adding it changes no behaviour.

Why it exists (design: docs/reference/trust_model_design.md §2-§4, B4):
the current merge encodes *precedence by call order* (fill-blank /
first-writer-wins), which conflates "which source is authoritative" with "which
ran first". This replaces it with **collect proposals -> resolve per field by
trust**:

- Each source emits ``FieldProposal(field, value, trust, source)`` objects.
- ``resolve()`` picks the highest-trust proposal *per field* (independent of
  collection order); equal-trust competing values are retained as alternatives
  (the seat of the future menu-alternatives feature, B5).
- ``disc_from_resolution()`` assembles the winners onto the base disc with a
  single ``dataclasses.replace`` call.

Two structural defect classes close **by construction** here:

- **C1** (physical fields silently dropped) — the physical/producer-only fields
  (``pre_emphasis``, ``low_dynamic_range``, ``cdtext_catalog_ref``,
  ``original_release_*``) are simply **not members of** :class:`Field`, so no
  metadata source can propose — and therefore cannot drop or fake — them. The
  assembler uses ``replace``, so they are carried over verbatim.
- **C2** (recording-level ``mb_release_id`` leaks) — a recording-level source
  (ISRC tally, AcoustID, stage-7 duration) raises at :class:`FieldProposal`
  construction if it tries to propose ``MB_RELEASE_ID``. The invariant is a type
  error, not a discipline.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from enum import Enum, IntEnum

from cdda2img.rbi_format import RBIDisc


class Trust(IntEnum):
    """Trust ladder. Higher wins. Assigned per ``(source, field, pipeline)`` by
    the Phase-B adapter; the values here are the *levels*, not the assignment."""

    MANUAL = 127  # user menu entry — always wins
    OBJECTIVE = 100  # physical/computed (Q-channel ISRC/MCN on the rip path)
    DISC_ID = 80  # MB release matched by disc-ID fingerprint
    ISRC = 70  # resolved/corroborated by per-track ISRC
    ACOUSTID = 60  # AcoustID fingerprint
    DISCOGS = 55  # Discogs catalogue / label / country
    DURATION = 40  # MB text + duration fuzzy (stage-7)
    CDTEXT = 35  # on-disc CD-Text baseline (rip)
    CDDB = 20  # gnudb free text (lowest network source)
    BASELINE = 10  # generic source-disc baseline below all network sources
    # The create-path mutagen baseline is lifted toward MANUAL by the Phase-B
    # adapter (the user's curated tags should win); the rip-path CD-Text baseline
    # enters at CDTEXT/BASELINE. Same producer, different trust per pipeline.


class Source(Enum):
    """Origin of a proposal. Used to enforce C2 and (in Phase B) to assign trust."""

    BASELINE = "baseline"  # CD-Text (rip) / mutagen tags (create) / foreign image
    MB_DISC_ID = "mb_disc_id"
    ISRC = "isrc"  # per-track ISRC tally — recording-level
    ACOUSTID = "acoustid"  # fingerprint — recording-level
    DISCOGS = "discogs"
    DURATION = "duration"  # stage-7 text+duration — recording-level
    CDDB = "cddb"
    CANONICAL_BARCODE = (
        "canonical_barcode"  # §10 _pick_canonical_barcode verdict (barcode only)
    )
    MENU = "menu"  # B-5: user-selected result in the interactive menu. NOT
    # recording-level: the user explicitly endorsed this release, so its
    # mb_release_id is authoritative (user-confirmed) and is kept, not C2-stripped.


# Sources that identify a *recording*, not a disc-ID-verified pressing. They must
# never propose a pressing-level mb_release_id (defect class C2).
_RECORDING_LEVEL: frozenset[Source] = frozenset({
    Source.ISRC,
    Source.ACOUSTID,
    Source.DURATION,
})


class Field(Enum):
    """The *mergeable metadata* fields. Each value is the exact ``RBIDisc`` /
    ``RBITocEntry`` attribute name, so the assembler can ``replace`` by it.

    Physical/producer-only fields (``pre_emphasis``, ``low_dynamic_range``,
    ``cdtext_catalog_ref``, ``original_release_*``) are deliberately absent — that
    omission *is* the C1 enforcement.
    """

    # disc-level
    ALBUM = "album"
    ARTIST = "artist"
    CATALOG = "catalog"  # on-disc MCN (archival; baseline passthrough only)
    BARCODE = "barcode"  # service UPC/EAN — the disambiguation key
    CATALOG_NUMBER = "catalog_number"  # label's own number
    LABEL = "label"
    COUNTRY = "country"
    RELEASE_DATE = "release_date"
    ORIGINAL_RELEASE_DATE = (
        "original_release_date"  # release-group first date (from MB)
    )
    DISC_NUMBER = "disc_number"
    DISC_TOTAL = "disc_total"
    SET_TITLE = "set_title"
    MB_RELEASE_ID = "mb_release_id"
    MB_RELEASE_GROUP_ID = "mb_release_group_id"
    DISCOGS_RELEASE_ID = "discogs_release_id"
    # track-level (require a track_number on the proposal)
    TRACK_TITLE = "title"
    TRACK_PERFORMER = "performer"
    TRACK_ISRC = "isrc"


_TRACK_FIELDS: frozenset[Field] = frozenset({
    Field.TRACK_TITLE,
    Field.TRACK_PERFORMER,
    Field.TRACK_ISRC,
})


@dataclass(frozen=True)
class FieldProposal:
    """One source's proposed value for one field (per-track when track-level).

    Construction enforces C1/C2 and the disc/track-level keying invariant.
    """

    field: Field
    value: object
    trust: Trust
    source: Source
    track_number: int | None = None

    def __post_init__(self) -> None:
        # C2: recording-level sources may not assert a pressing-level release.
        if self.field is Field.MB_RELEASE_ID and self.source in _RECORDING_LEVEL:
            msg = (
                f"C2 violation: {self.source.value} is recording-level and may "
                "not propose mb_release_id (use mb_release_group_id)"
            )
            raise ValueError(msg)
        # Disc/track-level keying must match the field's level.
        if self.field in _TRACK_FIELDS and self.track_number is None:
            msg = f"{self.field.name} is track-level; track_number is required"
            raise ValueError(msg)
        if self.field not in _TRACK_FIELDS and self.track_number is not None:
            msg = f"{self.field.name} is disc-level; track_number must be None"
            raise ValueError(msg)


# (Field, track_number) — track_number is None for disc-level fields.
ProposalKey = tuple[Field, int | None]


@dataclass(frozen=True)
class Resolution:
    """The outcome of :func:`resolve`.

    ``winners`` maps each contested key to the highest-trust proposal.
    ``alternatives`` maps a key to the *other* distinct-valued proposals (highest
    trust per distinct value, descending) — the B5 menu view.
    ``contenders`` maps a key to **every** non-empty proposal it received (trust
    descending, including the winner and same-valued agreers) — the full decision
    trace for "why did this field resolve to X?" (§11.5 traceability). When two
    menu choices are both wrong, this is what shows whether the right value was
    never proposed, or proposed-but-outranked, and by which source/trust.
    ``skipped`` maps a key to the proposals :func:`resolve` **discarded before
    ranking**, each with the reason — the third case the first two cannot express.

    The three answer different questions and the third is the one that was
    missing (§11.5). "Why is this field X?" is ``contenders``. "What else could it
    be?" is ``alternatives``. But a value that was *dropped* appears in neither:
    it is absent from both, exactly as if the source had never proposed it. So a
    correct value discarded as an empty string, or as an "Unknown Artist"
    sentinel, was indistinguishable from one that was never offered — and those
    call for opposite fixes (mend the filter vs. mend the source).
    """

    winners: dict[ProposalKey, FieldProposal] = dc_field(default_factory=dict)
    alternatives: dict[ProposalKey, tuple[FieldProposal, ...]] = dc_field(
        default_factory=dict
    )
    contenders: dict[ProposalKey, tuple[FieldProposal, ...]] = dc_field(
        default_factory=dict
    )
    skipped: dict[ProposalKey, tuple[tuple[FieldProposal, str], ...]] = dc_field(
        default_factory=dict
    )


def _is_empty(value: object) -> bool:
    """A proposal carries no information if its value is None or an empty string.

    Sources should only propose fields they actually have; this is a defensive
    skip so a high-trust *blank* can never beat a low-trust real value (the
    fill-blank intent, preserved)."""
    return value is None or value == ""


def _skip_reason(value: object) -> str | None:
    """Why this proposal is discarded before ranking, or ``None`` to keep it.

    Deliberately returns a *reason* rather than a boolean. The boolean form is
    what made a dropped value invisible: `resolve` could say "this key has no
    proposals" but never "it had one and I threw it away, for this". Those need
    opposite fixes — mend the filter, or mend the source — and the caller cannot
    choose between them without the reason.
    """
    if value is None:
        return "none"
    if value == "":
        return "empty"
    return None


def resolve(proposals: Iterable[FieldProposal]) -> Resolution:
    """Resolve proposals to one winner per ``(field, track_number)`` by trust.

    Highest trust wins, independent of iteration order. Ties (equal trust, equal
    value) collapse to one winner; ties with *different* values keep first-seen as
    the winner and record the rest as alternatives. Empty/None-valued proposals are
    ignored (see :func:`_is_empty`) — but **recorded** in ``skipped`` rather than
    dropped without trace, so a discarded value can be told apart from one that
    was never proposed.
    """
    by_key: dict[ProposalKey, list[FieldProposal]] = defaultdict(list)
    dropped: dict[ProposalKey, list[tuple[FieldProposal, str]]] = defaultdict(list)
    for p in proposals:
        reason = _skip_reason(p.value)
        if reason is not None:
            dropped[(p.field, p.track_number)].append((p, reason))
            continue
        by_key[(p.field, p.track_number)].append(p)

    winners: dict[ProposalKey, FieldProposal] = {}
    alternatives: dict[ProposalKey, tuple[FieldProposal, ...]] = {}
    contenders: dict[ProposalKey, tuple[FieldProposal, ...]] = {}
    for key, props in by_key.items():
        # Full trace: every non-empty proposal for this key, trust descending.
        ranked = sorted(props, key=lambda p: p.trust, reverse=True)
        contenders[key] = tuple(ranked)

        # max() returns the first element achieving the max trust, so equal-trust
        # ties break by first-seen — deterministic, and lets Phase B reproduce
        # today's order by encoding it as distinct trust values.
        winner = max(props, key=lambda p: p.trust)
        winners[key] = winner

        # Alternatives (B5 view): distinct values other than the winner's, best
        # trust each.
        best_by_value: dict[object, FieldProposal] = {}
        for p in props:
            if p.value == winner.value:
                continue
            cur = best_by_value.get(p.value)
            if cur is None or p.trust > cur.trust:
                best_by_value[p.value] = p
        if best_by_value:
            alternatives[key] = tuple(
                sorted(best_by_value.values(), key=lambda p: p.trust, reverse=True)
            )

    return Resolution(
        winners=winners,
        alternatives=alternatives,
        contenders=contenders,
        skipped={k: tuple(v) for k, v in dropped.items()},
    )


def disc_from_resolution(resolution: Resolution, base: RBIDisc) -> RBIDisc:
    """Assemble the resolved winners onto *base* via a single ``replace``.

    Physical/producer-only fields are not in :class:`Field`, so they are never in
    *resolution* and are carried over from *base* untouched (C1). Track-level
    winners are applied per ``track_number``; tracks with no winners are kept
    verbatim.
    """
    disc_changes: dict[str, object] = {}
    track_changes: dict[int, dict[str, object]] = defaultdict(dict)
    for (fld, tno), winner in resolution.winners.items():
        if tno is None:
            disc_changes[fld.value] = winner.value
        else:
            track_changes[tno][fld.value] = winner.value

    new_tracks = [
        replace(t, **track_changes[t.track_number])
        if t.track_number in track_changes
        else t
        for t in base.tracks
    ]
    return replace(base, tracks=new_tracks, **disc_changes)
