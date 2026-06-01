"""Tests for swing exit rule selection."""

import pytest

from strategies.exit_rules import choose_exit, compute_lock_profit, compute_sell_profit
from models import Position


def _pos(entry=0.25, count=100):
    return Position(
        ticker="KXNBAGAME-TEST",
        title="Game Winner",
        side="yes",
        entry_price=entry,
        count=count,
        sport="nba",
    )


class TestChooseExit:
    def test_sell_mode_5pct(self):
        cfg = {"exit_mode": "sell", "min_sell_profit_pct": 0.05}
        pos = _pos(0.25, 100)
        cost = 25.0
        sell_p = compute_sell_profit(pos, 0.30)  # $5 profit
        action, _ = choose_exit(cfg, sell_p, 0, cost)
        assert action == "sell"

    def test_sell_mode_below_threshold(self):
        cfg = {"exit_mode": "sell", "min_sell_profit_pct": 0.10}
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.27)  # $2 = 8% of $25
        action, _ = choose_exit(cfg, sell_p, 0, 25)
        assert action is None

    def test_lock_mode_any_profit(self):
        cfg = {"exit_mode": "lock", "min_lock_profit_usd": 0.01}
        pos = _pos(0.25, 100)
        lock_p = compute_lock_profit(pos, 0.50)
        action, _ = choose_exit(cfg, 0, lock_p, 25)
        assert action == "lock"

    def test_hybrid_sell_first(self):
        cfg = {
            "exit_mode": "hybrid",
            "hybrid_style": "sell_first",
            "min_sell_profit_pct": 0.05,
            "min_lock_profit_usd": 0.01,
        }
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.35)
        lock_p = compute_lock_profit(pos, 0.40)
        action, _ = choose_exit(cfg, sell_p, lock_p, 25)
        assert action == "sell"

    def test_hybrid_greedy_picks_higher(self):
        cfg = {
            "exit_mode": "hybrid",
            "hybrid_style": "greedy",
            "min_sell_profit_pct": 0.05,
            "min_lock_profit_usd": 5,
        }
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.30)  # $5
        lock_p = compute_lock_profit(pos, 0.35)  # higher lock profit
        action, _ = choose_exit(cfg, sell_p, lock_p, 25)
        assert action == "lock"
