"""
test_field_resolver.py — B4 Phase A: the collect->resolve trust resolver.

Covers the primitives (not yet wired into the pipeline): FieldProposal
construction invariants (C1/C2 by construction), resolve() trust/order/tie
semantics, and disc_from_resolution() assembly + physical-field preservation.
"""

import pytest

from cdda2img.field_resolver import (
    Field,
    FieldProposal,
    Resolution,
    Source,
    Trust,
    disc_from_resolution,
    resolve,
)
from cdda2img.rbi_format import RBIDisc, RBITocEntry


def _p(field, value, trust, source, track_number=None) -> FieldProposal:
    return FieldProposal(
        field=field, value=value, trust=trust, source=source, track_number=track_number
    )


# === FieldProposal construction — C1/C2 by construction =====================


def test_c2_recording_level_source_cannot_propose_mb_release_id():
    for src in (Source.ACOUSTID, Source.ISRC, Source.DURATION):
        with pytest.raises(ValueError, match="C2 violation"):
            _p(Field.MB_RELEASE_ID, "rel-uuid", Trust.ISRC, src)


def test_disc_id_source_may_propose_mb_release_id():
    p = _p(Field.MB_RELEASE_ID, "rel-uuid", Trust.DISC_ID, Source.MB_DISC_ID)
    assert p.value == "rel-uuid"


def test_recording_level_source_may_propose_release_group():
    # The release *group* is not pressing-level — allowed from a recording source.
    p = _p(Field.MB_RELEASE_GROUP_ID, "rg-uuid", Trust.ACOUSTID, Source.ACOUSTID)
    assert p.value == "rg-uuid"


def test_track_field_requires_track_number():
    with pytest.raises(ValueError, match="track-level"):
        _p(Field.TRACK_TITLE, "Song", Trust.DISC_ID, Source.MB_DISC_ID)


def test_disc_field_rejects_track_number():
    with pytest.raises(ValueError, match="disc-level"):
        _p(Field.ALBUM, "X", Trust.DISC_ID, Source.MB_DISC_ID, track_number=1)


def test_physical_fields_are_not_proposable():
    # C1: there is no Field member for pre_emphasis / cdtext_catalog_ref etc.
    names = {f.name for f in Field}
    for physical in ("PRE_EMPHASIS", "LOW_DYNAMIC_RANGE", "CDTEXT_CATALOG_REF"):
        assert physical not in names


# === resolve() — highest trust wins, order-independent ======================


def test_highest_trust_wins():
    res = resolve([
        _p(Field.ALBUM, "JOSHUA TREE", Trust.CDTEXT, Source.BASELINE),
        _p(Field.ALBUM, "The Joshua Tree", Trust.DISC_ID, Source.MB_DISC_ID),
    ])
    assert res.winners[(Field.ALBUM, None)].value == "The Joshua Tree"


def test_resolution_is_order_independent():
    a = _p(Field.ALBUM, "JOSHUA TREE", Trust.CDTEXT, Source.BASELINE)
    b = _p(Field.ALBUM, "The Joshua Tree", Trust.DISC_ID, Source.MB_DISC_ID)
    assert (
        resolve([a, b]).winners[(Field.ALBUM, None)].value
        == resolve([b, a]).winners[(Field.ALBUM, None)].value
        == "The Joshua Tree"
    )


def test_empty_high_trust_does_not_beat_real_low_trust():
    # A blank proposal carries no info even at higher trust (fill-blank intent).
    res = resolve([
        _p(Field.ALBUM, "", Trust.DISC_ID, Source.MB_DISC_ID),
        _p(Field.ALBUM, "Real Album", Trust.CDTEXT, Source.BASELINE),
    ])
    assert res.winners[(Field.ALBUM, None)].value == "Real Album"


def test_equal_trust_different_value_first_seen_wins_other_is_alternative():
    first = _p(Field.ALBUM, "First", Trust.DISCOGS, Source.DISCOGS)
    second = _p(Field.ALBUM, "Second", Trust.DISCOGS, Source.DISCOGS)
    res = resolve([first, second])
    assert res.winners[(Field.ALBUM, None)].value == "First"
    alts = res.alternatives[(Field.ALBUM, None)]
    assert [a.value for a in alts] == ["Second"]


def test_alternatives_dedupe_by_value_keep_best_trust():
    res = resolve([
        _p(Field.ALBUM, "Winner", Trust.DISC_ID, Source.MB_DISC_ID),
        _p(Field.ALBUM, "Other", Trust.CDDB, Source.CDDB),
        _p(Field.ALBUM, "Other", Trust.DISCOGS, Source.DISCOGS),  # higher
    ])
    alts = res.alternatives[(Field.ALBUM, None)]
    assert len(alts) == 1
    assert alts[0].value == "Other" and alts[0].trust == Trust.DISCOGS


def test_track_level_keys_resolve_independently():
    res = resolve([
        _p(Field.TRACK_TITLE, "One", Trust.DISC_ID, Source.MB_DISC_ID, 1),
        _p(Field.TRACK_TITLE, "Two", Trust.DISC_ID, Source.MB_DISC_ID, 2),
    ])
    assert res.winners[(Field.TRACK_TITLE, 1)].value == "One"
    assert res.winners[(Field.TRACK_TITLE, 2)].value == "Two"


# === disc_from_resolution() — assembly + C1 preservation ====================


def _physical_disc() -> RBIDisc:
    return RBIDisc(
        album="OLD",
        artist="OLD",
        tracks=[
            RBITocEntry(
                track_number=1,
                title="old1",
                performer="p",
                start_frame=1000,
                duration_frames=500,
                pregap_frames=10,
                isrc=None,
            ),
            RBITocEntry(
                track_number=2,
                title="old2",
                performer="p",
                start_frame=2000,
                duration_frames=500,
                pregap_frames=10,
                isrc=None,
            ),
        ],
        pre_emphasis=True,
        low_dynamic_range=True,
        cdtext_catalog_ref="CID U2 6",
    )


def test_disc_from_resolution_applies_disc_and_track_winners():
    res = resolve([
        _p(Field.ALBUM, "New Album", Trust.DISC_ID, Source.MB_DISC_ID),
        _p(Field.TRACK_TITLE, "New One", Trust.DISC_ID, Source.MB_DISC_ID, 1),
    ])
    out = disc_from_resolution(res, _physical_disc())
    assert out.album == "New Album"
    assert out.tracks[0].title == "New One"
    assert out.tracks[1].title == "old2"  # untouched track kept verbatim
    # track timing untouched by a metadata merge
    assert out.tracks[0].start_frame == 1000 and out.tracks[0].duration_frames == 500


def test_disc_from_resolution_preserves_physical_fields():
    """C1: fields not in the Field enum survive the assembly verbatim."""
    res = resolve([_p(Field.ALBUM, "New", Trust.DISC_ID, Source.MB_DISC_ID)])
    out = disc_from_resolution(res, _physical_disc())
    assert out.pre_emphasis is True
    assert out.low_dynamic_range is True
    assert out.cdtext_catalog_ref == "CID U2 6"


def test_empty_resolution_is_identity_on_metadata():
    out = disc_from_resolution(Resolution(), _physical_disc())
    assert out.album == "OLD" and out.artist == "OLD"
    assert out.pre_emphasis is True
