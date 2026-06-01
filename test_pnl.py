"""Tests for settlement P&L and moneyline market filtering."""

import pytest

from kalshi_client import (
    GAME_MONEYLINE_SERIES,
    is_moneyline_market,
    parse_settlement,
    settlement_payout_dollars,
)
from paper_executor import PaperExecutor


class TestSettlementPayout:
    def test_yes_wins(self):
        assert settlement_payout_dollars("yes", 10, 0.40, "yes") == 10.0

    def test_yes_loses(self):
        assert settlement_payout_dollars("yes", 10, 0.40, "no") == 0.0

    def test_void_returns_cost(self):
        assert settlement_payout_dollars("yes", 10, 0.40, "void") == 4.0

    def test_no_side_wins(self):
        assert settlement_payout_dollars("no", 5, 0.30, "no") == 5.0


class TestParseSettlement:
    def test_finalized_yes(self):
        info = parse_settlement({
            "ticker": "KXNBAGAME-TEST",
            "status": "finalized",
            "result": "yes",
        })
        assert info is not None
        assert info.result == "yes"

    def test_active_not_settled(self):
        assert parse_settlement({
            "ticker": "KXNBAGAME-TEST",
            "status": "active",
            "result": "",
        }) is None


class TestMoneylineFilter:
    def test_rejects_mve_parlay(self):
        assert not is_moneyline_market({
            "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026-ABC",
            "title": "yes Atlanta,yes Over 7.5 runs scored",
            "series_ticker": "KXMV",
        })

    def test_rejects_spread(self):
        assert not is_moneyline_market({
            "ticker": "KXNBAGAME-TEST",
            "title": "yes Cleveland wins by over 9.5 points",
            "series_ticker": "KXNBAGAME",
        })

    def test_accepts_game_winner(self):
        assert is_moneyline_market({
            "ticker": "KXNBAGAME-26MAY26SASOKC-SAS",
            "title": "Game 5: San Antonio at Oklahoma City Winner?",
            "series_ticker": "KXNBAGAME",
        })

    def test_series_mapping(self):
        assert GAME_MONEYLINE_SERIES["nba"] == "KXNBAGAME"
        assert GAME_MONEYLINE_SERIES["mlb"] == "KXMLBGAME"
        assert GAME_MONEYLINE_SERIES["nhl"] == "KXNHLGAME"


class TestPaperExecutor:
    def test_lock_credits_full_payout(self):
        paper = PaperExecutor(1000.0)
        paper.buy("T", "yes", 10, 0.40)
        bal_after_buy = paper.get_balance()
        paper.lock("T", "no", 10, 0.55)
        # Spent 4 on yes, 5.5 on no, credited 10 → net +0.5 locked profit
        assert paper.get_balance() == pytest.approx(bal_after_buy - 5.5 + 10.0)

    def test_settle_yes_win(self):
        paper = PaperExecutor(1000.0)
        paper.buy("T", "yes", 10, 0.40)
        paper.settle("T", "yes", 10, payout=10.0, entry_price=0.40, result="yes")
        assert paper.get_balance() == pytest.approx(1000.0 - 4.0 + 10.0)
        log = paper.get_trade_log()
        assert log[-1]["action"] == "settle"
        assert log[-1]["profit"] == pytest.approx(6.0)
