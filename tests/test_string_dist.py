"""Tests for cdda2img.string_dist — pattern-weighted Levenshtein."""

import pytest

from cdda2img.string_dist import string_dist


def test_identical_strings_are_zero():
    assert string_dist("Eliminator", "Eliminator") == 0.0


def test_none_none_is_zero():
    assert string_dist(None, None) == 0.0


def test_none_vs_string_is_one():
    assert string_dist(None, "Eliminator") == 1.0
    assert string_dist("Eliminator", None) == 1.0


def test_case_insensitive():
    assert string_dist("ELIMINATOR", "eliminator") == 0.0


def test_article_end_word_convention():
    # "Something, The" is the same as "The Something"
    assert string_dist("Love, The", "The Love") == 0.0
    assert string_dist("Something, a", "A Something") == 0.0


def test_ampersand_expansion():
    # "&" → "and" before comparison
    assert string_dist("Simon & Garfunkel", "Simon and Garfunkel") == 0.0


def test_ep_suffix_ignored():
    # "(EP)" / "(Single)" have zero weight — completely ignored
    d = string_dist("Divine Madness EP", "Divine Madness")
    assert d < 0.05


def test_feat_suffix_low_weight():
    # "(feat. ...)" has weight 0.1 — very low penalty
    d = string_dist("Song (feat. Someone)", "Song")
    assert d < 0.15


def test_parenthetical_low_weight():
    # Parenthetical remarks at weight 0.3
    d = string_dist("Album (Reissue)", "Album")
    assert d < 0.35


def test_completely_different_strings_near_one():
    d = string_dist("Eliminator", "Purple Rain")
    assert d > 0.5


def test_minor_punctuation_difference_low():
    # Apostrophe/hyphen differences should be near zero (stripped in _string_dist_basic)
    d = string_dist("Don't Stop Me Now", "Dont Stop Me Now")
    assert d < 0.05


def test_unicode_normalisation():
    # Accented chars normalised via NFKD ASCII fold
    d = string_dist("Café", "Cafe")
    assert d < 0.05


def test_threshold_distinguishes_minor_vs_major():
    # Minor difference (same album, slightly different subtitle)
    minor = string_dist("Eliminator (1983)", "Eliminator")
    # Major difference (completely different titles)
    major = string_dist("Eliminator", "Dark Side of the Moon")
    assert minor < 0.15 < major


@pytest.mark.parametrize(
    "a,b,expected_max",
    [
        ("The Beatles", "Beatles, The", 0.05),  # end-word convention
        ("Highway to Hell", "Highway To Hell", 0.01),  # capitalisation only
        ("Pt. 1", "Part 1", 0.20),  # "part N" pattern, low weight
        ("Led Zeppelin IV", "Led Zeppelin 4", 0.20),  # numeral vs digit
    ],
)
def test_parametrized_common_cases(a, b, expected_max):
    assert string_dist(a, b) <= expected_max
