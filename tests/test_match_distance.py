"""Tests for cdda2img.match_distance — confidence scoring and recommendation."""

import ast
from pathlib import Path

import pytest

from cdda2img.match_distance import (
    MatchRecommendation,
    build_match_distance,
    final_match_distance,
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


# ---------------------------------------------------------------------------
# final_match_distance — the score that reaches the container (N6)
#
# `build_match_distance` scores the automatic GUESS and is printed before the
# menu; `final_match_distance` scores what the container ended up believing and
# is stored after it. These tests pin that they are allowed to differ, and by
# how much — the defect N6 fixes was that they were one call.
# ---------------------------------------------------------------------------


def test_a_manual_pressing_choice_short_circuits_to_certainty():
    """kgr's ruling, 2026-08-13: a manual selection is not a guess.

    Note this REPLACES the scorer rather than adding to it. The disc below would
    score 0.30 on the `mb_disc_id_multi` rung, and a purely additive
    "user_confirmed" contributor would land somewhere in between; the rule says
    the "how MB found it" axis does not run at all once a human has chosen.
    """
    disc = _disc(mb_release_id="picked-by-hand")
    prov = {"release_selected_via": "barcode_plurality", "release_selection": "manual"}

    guess = build_match_distance(disc, prov)
    final = final_match_distance(disc, prov)

    assert guess.score == pytest.approx(0.30), "premise: the guess was a weak one"
    assert final.score == 1.0
    assert final.recommendation == MatchRecommendation.STRONG
    assert final.contributors == {"user_confirmed": 1.0}
    assert "user_confirmed" in final.summary()


@pytest.mark.parametrize("outcome", ["unique", "auto_tiebreak", "rejected"])
def test_every_non_manual_outcome_falls_through_to_the_guess(outcome: str) -> None:
    """Only `manual` short-circuits. The other three are still guesses.

    `rejected` is included deliberately and is the known gap: the user said none
    of the listed pressings match, but `PressingScreen` keeps the automatic pick
    and does not clear `mb_release_id`, so the score is identical to an
    *un-reviewed* `auto_tiebreak`. That loses the fact that a human looked and
    said no. Pinned as CURRENT BEHAVIOUR, not as desired behaviour — kgr's ruling
    covers `manual` and leaves this open rather than inventing a value.
    """
    disc = _disc(mb_release_id="mbid")
    prov = {"release_selected_via": "date", "release_selection": outcome}

    assert final_match_distance(disc, prov).score == pytest.approx(
        build_match_distance(disc, prov).score
    )


def test_rejected_and_auto_tiebreak_are_indistinguishable_by_score():
    """The gap above, stated as its own claim so it cannot be silently closed.

    If someone later gives `rejected` a value of its own, this test fails and
    names the decision — which is the point. It is not asserting that today's
    behaviour is right.
    """
    disc = _disc(mb_release_id="mbid")
    base = {"release_selected_via": "date"}

    rejected = final_match_distance(disc, {**base, "release_selection": "rejected"})
    tiebreak = final_match_distance(
        disc, {**base, "release_selection": "auto_tiebreak"}
    )

    assert rejected.score == tiebreak.score == pytest.approx(0.30)


def test_an_absent_release_selection_is_not_an_error():
    """The create path never writes `release_selection` at all (it passes no
    `provenance=` to the menu), and neither does a disc MB has never heard of."""
    disc = _disc(mb_release_id="mbid")
    assert final_match_distance(disc, {}).score == pytest.approx(0.50)
    assert final_match_distance(_disc(), {}).score == 0.0


def test_the_duration_match_rung_fires_now_that_the_score_runs_after_the_menu():
    """+0.20 `mb_duration_match` was unreachable before N6 and had never executed.

    Stage 7 routes through `mb_lookup.strip_pressing_mbid`, which nulls
    `mb_release_id` precisely so a recording-level source cannot assert a
    pressing — so the guard `disc.mb_release_id and "duration_match_release" in
    prov` could not be true at the pre-menu call site. Running the score after
    the menu is what makes the state reachable, because the menu can set the id.

    This test asserts the branch is CORRECT, not merely present: 0.20 (the
    weakest MB rung), and specifically not the 0.50 unique-disc-ID rung, which is
    what it would score if the `duration_match_release` guard were checked in the
    wrong order.
    """
    disc = _disc(mb_release_id="set-by-the-menu")
    md = final_match_distance(disc, {"duration_match_release": "some-release"})

    assert md.contributors == {"mb_duration_match": 0.20}
    assert md.recommendation == MatchRecommendation.LOW


def test_duration_match_outranks_the_multi_pressing_rung_when_both_are_present():
    """Guard-order check. `duration_match_release` is tested first, so a disc
    carrying both keys scores the weaker 0.20 rather than 0.30 — correct, because
    a duration fuzzy is the weaker provenance and the score is a floor claim."""
    disc = _disc(mb_release_id="mbid")
    md = final_match_distance(
        disc,
        {"duration_match_release": "rel", "release_selected_via": "barcode_plurality"},
    )
    assert md.contributors == {"mb_duration_match": 0.20}


# ---------------------------------------------------------------------------
# Wiring guards (N6) — the defect was ORDERING, so pin the ordering
#
# The unit tests above prove `final_match_distance` returns the right number.
# They cannot catch the actual N6 bug, which was that the right number was
# computed at the wrong MOMENT: `build_match_distance` ran at cdda2img.py:2321
# and `run_metadata_menu` at :2349, so the stored key described a state the menu
# then superseded. A future edit re-adding a pre-menu write would pass every
# test above.
#
# Source-level guards, in the idiom `test_depcheck.py` already uses for its
# stdlib-only rule: an ordinary behavioural test cannot see call ORDER inside a
# 200-line pipeline function without executing the whole pipeline.
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parent.parent / "src" / "cdda2img" / "cdda2img.py"
_STORED_KEYS = {"match_confidence", "match_recommendation"}


def _tree() -> ast.Module:
    return ast.parse(_SRC.read_text(encoding="utf-8"))


def _enclosing_function(tree: ast.Module, lineno: int) -> str | None:
    """Innermost `def` containing `lineno`, by line span."""
    best: tuple[int, str] | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end and (best is None or node.lineno > best[0]):
                best = (node.lineno, node.name)
    return best[1] if best else None


def test_the_stored_match_keys_have_exactly_one_writer():
    """`_store_match_distance` is the only thing that writes them.

    Two writers for one fact is how the pre-menu and post-menu numbers diverged
    in the first place. If this fails, read the new write site before "fixing"
    the test: a second writer is the regression, not this assertion.
    """
    tree = _tree()
    writers: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "provenance"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in _STORED_KEYS
            ):
                writers.append((
                    target.slice.value,
                    _enclosing_function(tree, node.lineno),
                ))

    assert writers, "premise: the keys are written somewhere in cdda2img.py"
    assert {k for k, _ in writers} == _STORED_KEYS, (
        f"both keys must still be written; found {sorted({k for k, _ in writers})}"
    )
    assert {fn for _, fn in writers} == {"_store_match_distance"}, (
        f"match_confidence/match_recommendation written outside the single "
        f"writer: {sorted({fn for _, fn in writers})}"
    )


def test_the_score_is_stored_after_the_menu_on_every_path():
    """In each function that runs the menu, the store comes after it.

    This is N6 in one assertion. Both call sites are checked by construction
    rather than by name, so a third pipeline added later is covered without
    anyone remembering to extend the list.
    """
    tree = _tree()
    checked: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        menu = [
            c.lineno
            for c in ast.walk(node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "run_metadata_menu"
        ]
        store = [
            c.lineno
            for c in ast.walk(node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "_store_match_distance"
        ]
        if not menu:
            continue
        assert store, (
            f"{node.name} runs the metadata menu but never stores a post-menu "
            f"match distance — the N6 defect, reintroduced"
        )
        assert min(store) > max(menu), (
            f"{node.name} stores the match distance at line {min(store)}, "
            f"before the menu closes at line {max(menu)}"
        )
        checked.append(node.name)

    assert sorted(checked) == ["_finalize_import", "create_image"], (
        f"expected both pipelines to be covered, saw {sorted(checked)}"
    )
