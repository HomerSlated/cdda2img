"""
test_merge_invariants.py — B1: enforce the C1/C2 metadata-merge defect classes.

C1 — a *metadata* merge/clear must never reset a *physical/derived* disc field
(pre_emphasis, low_dynamic_range, cdtext_catalog_ref, disc layout, track timing).
The known merge sites use ``dataclasses.replace`` so they preserve these by
construction; these tests are the regression guard so a future hand-built
``RBIDisc(...)`` at any of these sites cannot silently drop them again.

C2 — a *recording-level* match (ISRC tally, stage-7 duration match, AcoustID)
must not write a pressing-level ``mb_release_id`` as if disc-ID-proven. The single
``strip_pressing_mbid`` chokepoint nulls it while keeping ``mb_release_group_id``.

See docs/reference/trust_model_design.md (B1) and the TODO "Structural" item.
"""

from unittest.mock import patch

from cdda2img.lookup_result import DiscMeta
from cdda2img.mb_lookup import (
    _merge_into_disc,
    _overwrite_disc,
    _resolve_via_isrc_tally,
    strip_pressing_mbid,
)
from cdda2img.metadata_menu import _clear_disc
from cdda2img.rbi_format import RBIDisc, RBITocEntry

# Every physical/derived field a metadata merge must carry over verbatim.
_PHYSICAL = {
    "pre_emphasis": True,
    "low_dynamic_range": True,
    "cdtext_catalog_ref": "CID U2 6",
    "disc_number": 2,
    "disc_total": 3,
    "set_title": "Box Set",
}


def _toc(n: int) -> RBITocEntry:
    return RBITocEntry(
        track_number=n,
        title=f"Track {n}",
        performer="Performer",
        start_frame=n * 1000,
        duration_frames=500,
        pregap_frames=10,
        isrc=None,
    )


def _physical_disc() -> RBIDisc:
    return RBIDisc(
        album="Album",
        artist="Artist",
        catalog="0123456789012",
        tracks=[_toc(1), _toc(2)],
        pre_emphasis=True,
        low_dynamic_range=True,
        cdtext_catalog_ref="CID U2 6",
        disc_number=2,
        disc_total=3,
        set_title="Box Set",
    )


def _assert_physical_survived(out: RBIDisc) -> None:
    for name, value in _PHYSICAL.items():
        assert getattr(out, name) == value, f"physical field {name} not preserved"
    # Track timing/structure must be untouched by a metadata merge.
    assert [t.start_frame for t in out.tracks] == [1000, 2000]
    assert [t.duration_frames for t in out.tracks] == [500, 500]
    assert [t.pregap_frames for t in out.tracks] == [10, 10]


# --------------------------------------------------------------------------- C1


def test_merge_into_disc_preserves_physical_fields():
    """C1: fill-blank merge with a meta lacking physical fields keeps them."""
    out = _merge_into_disc(DiscMeta(album="X", artist="Y"), _physical_disc())
    _assert_physical_survived(out)


def test_overwrite_disc_preserves_physical_fields():
    """C1: 'Overwrite All' merge still preserves physical/derived fields."""
    out = _overwrite_disc(DiscMeta(album="X", artist="Y"), _physical_disc())
    _assert_physical_survived(out)


def test_clear_disc_preserves_physical_and_structure():
    """C1: clearing *metadata* must not reset physical fields or timing."""
    out = _clear_disc(_physical_disc())
    assert out.album == "" and out.artist == ""  # metadata cleared
    assert all(t.title == "" for t in out.tracks)  # per-track metadata cleared
    # pre_emphasis / disc layout / timing survive (set_title is preserved too).
    assert out.pre_emphasis is True
    assert out.disc_number == 2 and out.disc_total == 3
    assert out.set_title == "Box Set"
    assert [t.start_frame for t in out.tracks] == [1000, 2000]
    assert [t.duration_frames for t in out.tracks] == [500, 500]


# --------------------------------------------------------------------------- C2


def test_strip_pressing_mbid_nulls_release_keeps_group():
    """C2: the chokepoint nulls mb_release_id, keeps mb_release_group_id + rest."""
    meta = DiscMeta(
        album="A",
        artist="B",
        mb_release_id="release-uuid",
        mb_release_group_id="rg-uuid",
        discogs_release_id=123,
    )
    out = strip_pressing_mbid(meta)
    assert out.mb_release_id is None
    assert out.mb_release_group_id == "rg-uuid"  # release-group survives
    assert out.album == "A" and out.artist == "B" and out.discogs_release_id == 123


def test_resolve_via_isrc_tally_strips_pressing_mbid():
    """C2: the R4 ISRC-tally fallback returns a meta with mb_release_id nulled."""
    disc = RBIDisc(
        album="",
        artist="",
        tracks=[
            RBITocEntry(
                track_number=n,
                title="",
                performer="",
                start_frame=n * 1000,
                duration_frames=500,
                pregap_frames=0,
                isrc=f"USAR1040040{n}",
            )
            for n in range(1, 4)  # 3 ISRC-bearing tracks == _R4_MIN floor
        ],
    )
    # Every ISRC resolves to the same release → converges above the floor.
    hit = DiscMeta(mb_release_id="rel-uuid", mb_release_group_id="rg-uuid")
    with patch("cdda2img.mb_lookup.lookup_isrc", return_value=[hit]):
        result = _resolve_via_isrc_tally(disc)
    assert result is not None
    assert result.mb_release_id is None  # C2 enforced at the call site
    assert result.mb_release_group_id == "rg-uuid"
