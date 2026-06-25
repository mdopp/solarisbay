"""Unit tests for the shared lightly-fuzzy scorer (#591)."""

from __future__ import annotations

from solaris_chat.engine.fuzzy import FUZZY_THRESHOLD, fuzzy_score, tokens


def test_tokens_lowercases_and_splits():
    assert tokens("Hello World") == ["hello", "world"]
    assert tokens("Bohemian-Rhapsody!") == ["bohemian", "rhapsody"]
    assert tokens("") == []


def test_exact_match_scores_high():
    assert fuzzy_score("Queen", "Queen") > 0.9


def test_whole_word_containment():
    # 'joel' is a whole word in 'Billy Joel' — clears the threshold.
    assert fuzzy_score("Joel", "Billy Joel") >= FUZZY_THRESHOLD


def test_typo_clears_threshold():
    # 'Beatls' is a typo of the 'Beatles' word in 'The Beatles'.
    assert fuzzy_score("Beatls", "The Beatles") >= FUZZY_THRESHOLD


def test_unrelated_scores_low():
    assert fuzzy_score("xqzptv", "Billy Joel") < FUZZY_THRESHOLD


def test_empty_inputs_score_zero():
    assert fuzzy_score("", "Queen") == 0.0
    assert fuzzy_score("Queen", "") == 0.0


def test_prefix_breaks_ties():
    # Same whole-word match in both; the one STARTING with the query wins on the
    # prefix bonus.
    assert fuzzy_score("Apfel", "Apfel Kuchen") > fuzzy_score("Apfel", "Kuchen Apfel")
