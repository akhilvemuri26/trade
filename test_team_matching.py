"""Tests for ESPN team name matching."""

from team_matching import match_feed_team


def test_match_mlb_ticker_code():
    names = ["Cincinnati Reds", "Milwaukee Brewers"]
    assert match_feed_team("Cincinnati", "CIN", names, sport="mlb") == "Cincinnati Reds"
    assert match_feed_team("Milwaukee", "MIL", names, sport="mlb") == "Milwaukee Brewers"


def test_match_fuzzy_city():
    names = ["Boston Red Sox", "New York Yankees"]
    assert match_feed_team("Boston", "", names, sport="mlb") == "Boston Red Sox"


def test_match_returns_none_when_no_feed():
    assert match_feed_team("Boston", "BOS", [], sport="mlb") is None
