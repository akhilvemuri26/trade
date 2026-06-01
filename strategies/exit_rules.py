"""Price-swing exit rules for underdog moneyline strategies."""

from __future__ import annotations

from typing import Literal, Optional

from models import Position

ExitAction = Literal["sell", "lock"]


def entry_cost(position: Position) -> float:
    return position.entry_price * position.count


def compute_sell_profit(position: Position, yes_ask: float) -> float:
    return (yes_ask - position.entry_price) * position.count


def compute_lock_profit(position: Position, no_ask: float) -> float:
    return (1.0 - position.entry_price - no_ask) * position.count


def _meets_sell_threshold(config: dict, sell_profit: float, cost: float) -> bool:
    min_usd = config.get("min_sell_profit_usd")
    min_pct = config.get("min_sell_profit_pct")
    if min_usd is not None and sell_profit >= min_usd:
        return True
    if min_pct is not None and cost > 0 and sell_profit >= cost * min_pct:
        return True
    return False


def _meets_lock_threshold(config: dict, lock_profit: float, cost: float) -> bool:
    min_usd = config.get("min_lock_profit_usd")
    min_pct = config.get("min_lock_profit_pct")
    if min_usd is not None and lock_profit >= min_usd:
        return True
    if min_pct is not None and cost > 0 and lock_profit >= cost * min_pct:
        return True
    return False


def should_sell(config: dict, sell_profit: float, cost: float) -> bool:
    return _meets_sell_threshold(config, sell_profit, cost)


def should_lock(config: dict, lock_profit: float, cost: float) -> bool:
    return _meets_lock_threshold(config, lock_profit, cost)


def choose_exit(
    config: dict,
    sell_profit: float,
    lock_profit: float,
    cost: float,
) -> tuple[Optional[ExitAction], str]:
    """
    Return (action, reason) or (None, "") to hold.
    """
    mode = config.get("exit_mode", "hybrid")

    if mode == "sell":
        if should_sell(config, sell_profit, cost):
            return "sell", _sell_reason(config)
        return None, ""

    if mode == "lock":
        if should_lock(config, lock_profit, cost):
            return "lock", _lock_reason(config)
        return None, ""

    # hybrid
    hybrid_style = config.get("hybrid_style", "sell_first")

    if hybrid_style == "greedy":
        sell_ok = should_sell(config, sell_profit, cost)
        lock_ok = should_lock(config, lock_profit, cost)
        if sell_ok and lock_ok:
            if sell_profit >= lock_profit:
                return "sell", _sell_reason(config)
            return "lock", _lock_reason(config)
        if sell_ok:
            return "sell", _sell_reason(config)
        if lock_ok:
            return "lock", _lock_reason(config)
        return None, ""

    # sell_first (default): try sell thresholds, then lock
    if should_sell(config, sell_profit, cost):
        return "sell", _sell_reason(config)
    if should_lock(config, lock_profit, cost):
        return "lock", _lock_reason(config)
    return None, ""


def _sell_reason(config: dict) -> str:
    if config.get("min_sell_profit_usd") is not None:
        return f"swing_sell_{config['min_sell_profit_usd']}usd"
    if config.get("min_sell_profit_pct") is not None:
        return f"swing_sell_{int(config['min_sell_profit_pct'] * 100)}pct"
    return "swing_sell"


def _lock_reason(config: dict) -> str:
    if config.get("min_lock_profit_usd") is not None:
        return f"swing_lock_{config['min_lock_profit_usd']}usd"
    if config.get("min_lock_profit_pct") is not None:
        return f"swing_lock_{int(config['min_lock_profit_pct'] * 100)}pct"
    return "swing_lock"
