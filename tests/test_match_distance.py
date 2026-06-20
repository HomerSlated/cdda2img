"""Tests for cdda2img.match_distance — confidence scoring and recommendation."""

import pytest

from cdda2img.match_distance import (
    MatchRecommendation,
    build_match_distance,
)
from cdda2img.rbi_format import RBIDisc


def _disc(mb_release_id: str | None = None) -> RBIDisc:
    return RBIDisc(album="Album", artist="Artist", mb_release_id=mb_release_id)


# ---------------------------------------------------------------------------
# build_match_distance — score composition
# ---------------------------------------------------------------------------


def test_no_signals_is_zero_and_none():
    disc = _disc()
    md = build_match_distance(disc, {})
    assert md.score == 0.0
    assert md.recommendation == MatchRecommendation.NONE


def test_mb_disc_id_alone_is_medium():
    disc = _disc(mb_release_id="some-mbid")
    md = build_match_distance(disc, {})
    assert pytest.approx(md.score) == 0.50
    assert md.recommendation == MatchRecommendation.MEDIUM


def test_mb_duration_match_alone_is_low():
    disc = _disc(mb_release_id="some-mbid")
    md = build_match_distance(disc, {"duration_match_release": "some-mbid"})
    assert pytest.approx(md.score) == 0.20
    assert md.recommendation == MatchRecommendation.LOW


def test_disc_id_plus_acoustid_is_strong():
    disc = _disc(mb_release_id="some-mbid")
    prov = {"acoustid_corroborates": "YES"}
    md = build_match_distance(disc, prov)
    assert pytest.approx(md.score) == 0.75
    assert md.recommendation == MatchRecommendation.STRONG


def test_disc_id_plus_isrc_disambiguated_is_medium():
    disc = _disc(mb_release_id="some-mbid")
    prov = {"multi_match_isrc_disambiguated": "YES"}
    md = build_match_distance(disc, prov)
    assert pytest.approx(md.score) == 0.65
    assert md.recommendation == MatchRecommendation.MEDIUM


def test_disc_id_plus_acoustid_plus_isrc_is_strong():
    disc = _disc(mb_release_id="some-mbid")
    prov = {
        "acoustid_corroborates": "YES",
        "multi_match_isrc_disambiguated": "YES",
    }
    md = build_match_distance(disc, prov)
    assert pytest.approx(md.score) == 0.90
    assert md.recommendation == MatchRecommendation.STRONG


def test_rung_pinned_release_alone_is_low():
    # §10.3: a disc-ID multi-match pinned by the rung is weaker than a unique
    # disc-ID hit (0.30 vs 0.50) — album identified, pressing a best guess.
    disc = _disc(mb_release_id="some-mbid")
    md = build_match_distance(disc, {"release_selected_via": "preferred_country"})
    assert pytest.approx(md.score) == 0.30
    assert md.recommendation == MatchRecommendation.LOW
    assert "mb_disc_id_multi" in md.contributors
    assert "mb_disc_id" not in md.contributors


def test_rung_pinned_plus_acoustid_is_medium_not_strong():
    # The reported regression: before this, a rung-pinned release + AcoustID hit
    # 0.75 (STRONG) and auto-skipped the menu without --auto. Now 0.30 + 0.25 =
    # 0.55 (MEDIUM) so the menu is shown for the user to confirm the pressing.
    disc = _disc(mb_release_id="some-mbid")
    prov = {"release_selected_via": "date", "acoustid_corroborates": "YES"}
    md = build_match_distance(disc, prov)
    assert pytest.approx(md.score) == 0.55
    assert md.recommendation == MatchRecommendation.MEDIUM


def test_disagreement_penalty_reduces_score():
    disc = _disc(mb_release_id="some-mbid")
    prov = {"disagreement_cddb_mb": "album"}
    md = build_match_distance(disc, prov)
    # 0.50 - 0.10 = 0.40 → boundary of MEDIUM
    assert pytest.approx(md.score) == 0.40
    assert md.recommendation == MatchRecommendation.MEDIUM


def test_disagreement_cannot_push_below_zero():
    disc = _disc()
    prov = {"disagreement_cddb_mb": "album,artist"}
    md = build_match_distance(disc, prov)
    assert md.score == 0.0
    assert md.recommendation == MatchRecommendation.NONE


def test_acoustid_without_mb_is_low():
    disc = _disc()
    prov = {"acoustid_corroborates": "YES"}
    md = build_match_distance(disc, prov)
    assert pytest.approx(md.score) == 0.25
    assert md.recommendation == MatchRecommendation.LOW


# ---------------------------------------------------------------------------
# contributors dict — named signals
# ---------------------------------------------------------------------------


def test_contributors_named_correctly_for_disc_id():
    disc = _disc(mb_release_id="some-mbid")
    md = build_match_distance(disc, {})
    assert "mb_disc_id" in md.contributors
    assert "mb_duration_match" not in md.contributors


def test_contributors_named_correctly_for_duration_match():
    disc = _disc(mb_release_id="some-mbid")
    md = build_match_distance(disc, {"duration_match_release": "some-mbid"})
    assert "mb_duration_match" in md.contributors
    assert "mb_disc_id" not in md.contributors


# ---------------------------------------------------------------------------
# summary()
# ---------------------------------------------------------------------------


def test_summary_includes_recommendation_and_score():
    disc = _disc(mb_release_id="some-mbid")
    prov = {"acoustid_corroborates": "YES"}
    md = build_match_distance(disc, prov)
    s = md.summary()
    assert "strong" in s
    assert "0.75" in s
    assert "mb_disc_id" in s
    assert "acoustid" in s


def test_summary_no_signals():
    disc = _disc()
    md = build_match_distance(disc, {})
    assert "no positive signals" in md.summary()


# ---------------------------------------------------------------------------
# MatchRecommendation enum values
# ---------------------------------------------------------------------------


def test_recommendation_values():
    assert MatchRecommendation.STRONG.value == "strong"
    assert MatchRecommendation.MEDIUM.value == "medium"
    assert MatchRecommendation.LOW.value == "low"
    assert MatchRecommendation.NONE.value == "none"
