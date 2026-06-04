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


class TestStopLoss:
    def test_stop_loss_pct_triggers(self):
        # entry 0.25 x100 = $25 cost; price falls to 0.10 -> -$15 loss = 60% of cost
        cfg = {"exit_mode": "hold", "stop_loss_pct": 0.50}
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.10)  # -$15
        action, reason = choose_exit(cfg, sell_p, 0, 25)
        assert action == "sell"
        assert reason == "stop_loss_50pct"

    def test_stop_loss_usd_triggers(self):
        cfg = {"exit_mode": "hold", "stop_loss_usd": 10}
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.13)  # -$12 loss
        action, reason = choose_exit(cfg, sell_p, 0, 25)
        assert action == "sell"
        assert reason == "stop_loss_10usd"

    def test_stop_loss_not_triggered_in_profit(self):
        cfg = {"exit_mode": "hold", "stop_loss_pct": 0.50}
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.40)  # +$15 profit, not a loss
        action, _ = choose_exit(cfg, sell_p, 0, 25)
        assert action is None  # hold mode never takes profit

    def test_stop_loss_below_threshold_holds(self):
        cfg = {"exit_mode": "hold", "stop_loss_pct": 0.50}
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.20)  # -$5 = 20% of cost, under 50%
        action, _ = choose_exit(cfg, sell_p, 0, 25)
        assert action is None

    def test_stop_takes_priority_over_take_profit(self):
        # Stop must fire even when a sell threshold is also configured.
        cfg = {"exit_mode": "sell", "min_sell_profit_pct": 0.05, "stop_loss_pct": 0.40}
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.10)  # -$15 loss
        action, reason = choose_exit(cfg, sell_p, 0, 25)
        assert action == "sell"
        assert reason.startswith("stop_loss")

    def test_hold_mode_does_not_clip_winners(self):
        # The core fix: hold mode never takes profit, so winners ride to settlement.
        cfg = {"exit_mode": "hold"}
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.90)  # huge unrealized gain
        action, _ = choose_exit(cfg, sell_p, 1000, 25)
        assert action is None

    def test_no_stop_keys_is_backward_compatible(self):
        # Without a stop_loss_* key, a deep loss never triggers an early exit.
        cfg = {"exit_mode": "sell", "min_sell_profit_usd": 5}
        pos = _pos(0.25, 100)
        sell_p = compute_sell_profit(pos, 0.01)  # -$24 loss
        action, _ = choose_exit(cfg, sell_p, 0, 25)
        assert action is None


class TestTakeProfit:
    def test_take_profit_frac_triggers_near_resolution(self):
        # entry 0.40 x100 -> max_gain = 0.60*100 = $60; frac 0.80 -> need >= $48
        cfg = {"exit_mode": "take_profit", "take_profit_frac": 0.80}
        pos = _pos(0.40, 100)
        max_gain = (1.0 - 0.40) * 100
        sell_p = compute_sell_profit(pos, 0.90)  # +$50 >= $48
        action, reason = choose_exit(cfg, sell_p, 0, 40, max_gain=max_gain)
        assert action == "sell"
        assert reason == "take_profit_80pct_of_move"

    def test_take_profit_holds_until_most_of_move_captured(self):
        cfg = {"exit_mode": "take_profit", "take_profit_frac": 0.80}
        pos = _pos(0.40, 100)
        max_gain = (1.0 - 0.40) * 100
        sell_p = compute_sell_profit(pos, 0.80)  # +$40 < $48
        action, _ = choose_exit(cfg, sell_p, 0, 40, max_gain=max_gain)
        assert action is None

    def test_take_profit_requires_max_gain(self):
        # Omitting max_gain (e.g. an old caller) must not fire a take-profit.
        cfg = {"exit_mode": "take_profit", "take_profit_frac": 0.50}
        pos = _pos(0.40, 100)
        sell_p = compute_sell_profit(pos, 0.95)
        action, _ = choose_exit(cfg, sell_p, 0, 40)
        assert action is None

    def test_stop_loss_works_in_take_profit_mode(self):
        cfg = {"exit_mode": "take_profit", "take_profit_frac": 0.80, "stop_loss_pct": 0.50}
        pos = _pos(0.40, 100)
        max_gain = (1.0 - 0.40) * 100
        sell_p = compute_sell_profit(pos, 0.18)  # -$22 = 55% of $40 cost
        action, reason = choose_exit(cfg, sell_p, 0, 40, max_gain=max_gain)
        assert action == "sell"
        assert reason.startswith("stop_loss")
