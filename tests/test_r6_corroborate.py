"""Tests for cdda2img._r6_tally_and_merge AcoustID corroboration flag (BUG-3)."""

from __future__ import annotations

from types import SimpleNamespace

from cdda2img.cdda2img import _r6_tally_and_merge
from cdda2img.rbi_format import RBIDisc


def _hit(rid: str) -> SimpleNamespace:
    return SimpleNamespace(mb_release_id=rid)


def test_corroborates_by_membership_not_first_element() -> None:
    # BUG-3 regression: consistent_rids is built from a set (nondeterministic
    # order). When AcoustID converges on more than one consistent release and the
    # disc's own MBID is among them, the flag must be YES regardless of which one
    # happens to land at index 0.
    disc = RBIDisc(album="A", artist="B", mb_release_id="R-DISC")
    per_track_hits = [
        [_hit("R-OTHER"), _hit("R-DISC")],
        [_hit("R-DISC"), _hit("R-OTHER")],
    ]
    prov: dict[str, str] = {}
    _r6_tally_and_merge(per_track_hits, disc, prov)
    assert prov["acoustid_corroborates"] == "YES"


def test_does_not_corroborate_when_disc_mbid_inconsistent() -> None:
    disc = RBIDisc(album="A", artist="B", mb_release_id="R-DISC")
    # R-DISC appears on only one track -> not consistent -> NO.
    per_track_hits = [
        [_hit("R-OTHER"), _hit("R-DISC")],
        [_hit("R-OTHER")],
    ]
    prov: dict[str, str] = {}
    _r6_tally_and_merge(per_track_hits, disc, prov)
    assert prov["acoustid_corroborates"] == "NO"
