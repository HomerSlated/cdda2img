"""Tests for cdda2img._r6_tally_and_merge AcoustID corroboration flag (BUG-3)
and the §10.4 post-selection AcoustID gate."""

from __future__ import annotations

from types import SimpleNamespace

from cdda2img.cdda2img import _gate_adjusted_auto, _r6_tally_and_merge
from cdda2img.rbi_format import RBIDisc


def _hit(rid: str, rgid: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(mb_release_id=rid, mb_release_group_id=rgid)


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


# ---------------------------------------------------------------------------
# §10.4 AcoustID gate (release-group level, fail-only key)
# ---------------------------------------------------------------------------


def test_gate_passes_when_release_group_present() -> None:
    # disc album (release-group) appears in AcoustID results -> pass -> no key.
    disc = RBIDisc(
        album="A", artist="B", mb_release_id="R-DISC", mb_release_group_id="RG-DISC"
    )
    per_track_hits = [
        [_hit("R-DISC", "RG-DISC")],
        [_hit("R-DISC", "RG-DISC")],
    ]
    prov: dict[str, str] = {}
    _r6_tally_and_merge(per_track_hits, disc, prov)
    assert "acoustid_gate" not in prov


def test_gate_passes_edition_blind_release_id_mismatch() -> None:
    # The headline case for matching at release-group level: AcoustID found a
    # *different pressing* (release-id) of the *same album* (release-group).
    # release-id corroboration is NO, but the gate must PASS (not flag a fail).
    disc = RBIDisc(
        album="A", artist="B", mb_release_id="R-DISC", mb_release_group_id="RG-DISC"
    )
    per_track_hits = [
        [_hit("R-PRESSING-2", "RG-DISC")],
        [_hit("R-PRESSING-2", "RG-DISC")],
    ]
    prov: dict[str, str] = {}
    _r6_tally_and_merge(per_track_hits, disc, prov)
    assert prov["acoustid_corroborates"] == "NO"  # different release-id
    assert "acoustid_gate" not in prov  # but same album -> gate passes


def test_gate_passes_when_release_group_in_one_track() -> None:
    # >= 1 probed track suffices (union membership).
    disc = RBIDisc(
        album="A", artist="B", mb_release_id="R-DISC", mb_release_group_id="RG-DISC"
    )
    per_track_hits = [
        [_hit("R-X", "RG-OTHER")],
        [_hit("R-DISC", "RG-DISC")],
    ]
    prov: dict[str, str] = {}
    _r6_tally_and_merge(per_track_hits, disc, prov)
    assert "acoustid_gate" not in prov


def test_gate_fails_when_album_absent_from_acoustid() -> None:
    # AcoustID confidently identified a *different album* -> fail.
    disc = RBIDisc(
        album="A", artist="B", mb_release_id="R-DISC", mb_release_group_id="RG-DISC"
    )
    per_track_hits = [
        [_hit("R-OTHER", "RG-OTHER")],
        [_hit("R-OTHER", "RG-OTHER")],
    ]
    prov: dict[str, str] = {}
    _r6_tally_and_merge(per_track_hits, disc, prov)
    assert prov["acoustid_gate"] == "failed"


def test_gate_not_evaluated_without_disc_release_group() -> None:
    # No matched release-group on the disc -> nothing to gate against.
    disc = RBIDisc(album="A", artist="B", mb_release_id="R-DISC")
    per_track_hits = [[_hit("R-OTHER", "RG-OTHER")]]
    prov: dict[str, str] = {}
    _r6_tally_and_merge(per_track_hits, disc, prov)
    assert "acoustid_gate" not in prov


def test_gate_not_evaluated_without_acoustid_release_group_evidence() -> None:
    # AcoustID returned releases but none carried a release-group id -> we cannot
    # evaluate -> inconclusive (no key), NOT a fail.
    disc = RBIDisc(
        album="A", artist="B", mb_release_id="R-DISC", mb_release_group_id="RG-DISC"
    )
    per_track_hits = [[_hit("R-OTHER", None)], [_hit("R-OTHER", None)]]
    prov: dict[str, str] = {}
    _r6_tally_and_merge(per_track_hits, disc, prov)
    assert "acoustid_gate" not in prov


# ---------------------------------------------------------------------------
# _gate_adjusted_auto — warn-only suppression of --auto
# ---------------------------------------------------------------------------


def test_gate_adjusted_auto_suppresses_on_fail(capsys) -> None:
    assert _gate_adjusted_auto(True, {"acoustid_gate": "failed"}) is False
    out = capsys.readouterr().out
    assert "AcoustID gate" in out


def test_gate_adjusted_auto_passes_through_auto_true() -> None:
    assert _gate_adjusted_auto(True, {}) is True


def test_gate_adjusted_auto_passes_through_auto_false() -> None:
    # No gate failure, but --auto was not requested -> still no auto-apply.
    assert _gate_adjusted_auto(False, {}) is False
