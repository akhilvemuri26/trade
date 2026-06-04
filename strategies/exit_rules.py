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


def should_stop(config: dict, sell_profit: float, cost: float) -> bool:
    """True once the position's unrealized loss reaches the stop-loss threshold.

    `sell_profit` is (yes_bid - entry) * count, so a loss is a negative value.
    Cuts the losing tail that the swing-exit rules never close (they only ever
    take profit, so losers otherwise ride to a near-total settlement loss).
    """
    if sell_profit >= 0:
        return False
    loss = -sell_profit
    stop_usd = config.get("stop_loss_usd")
    stop_pct = config.get("stop_loss_pct")
    if stop_usd is not None and loss >= stop_usd:
        return True
    if stop_pct is not None and cost > 0 and loss >= cost * stop_pct:
        return True
    return False


def should_take_profit(config: dict, sell_profit: float, max_gain: Optional[float]) -> bool:
    """True once the position has captured `take_profit_frac` of the move to $1.

    `max_gain` is (1 - entry) * count — the profit if the contract settled YES.
    Unlike a %-of-cost target, this is price-independent: frac=0.8 always means
    "80% of the way from entry to a winning settlement", so it captures most of
    a winner without holding all the way through settlement risk.
    """
    frac = config.get("take_profit_frac")
    if frac is None or max_gain is None or max_gain <= 0:
        return False
    return sell_profit >= frac * max_gain


def choose_exit(
    config: dict,
    sell_profit: float,
    lock_profit: float,
    cost: float,
    *,
    max_gain: Optional[float] = None,
) -> tuple[Optional[ExitAction], str]:
    """
    Return (action, reason) or (None, "") to hold.

    `max_gain` (keyword-only) is the settle-YES profit (1-entry)*count, required
    only by the "take_profit" mode; omitting it leaves all other modes unchanged.
    """
    # Stop-loss has top priority: it cuts the losing tail and is executed as a
    # YES sell at the current bid (realizes the loss). Backward-compatible —
    # only fires when a stop_loss_* key is set on the config.
    if should_stop(config, sell_profit, cost):
        return "sell", _stop_reason(config)

    mode = config.get("exit_mode", "hybrid")

    # "hold": never take profit — winners ride to settlement (only the stop-loss
    # above can close early). Avoids truncating the winning tail, which the
    # backtest showed costs ~$14.6k across runs 2-3.
    if mode == "hold":
        return None, ""

    # "take_profit": sell once we've captured most of the move to $1, so we keep
    # the bulk of a winner instead of clipping it at a small fixed gain.
    if mode == "take_profit":
        if should_take_profit(config, sell_profit, max_gain):
            return "sell", _take_profit_reason(config)
        return None, ""

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


def _stop_reason(config: dict) -> str:
    if config.get("stop_loss_usd") is not None:
        return f"stop_loss_{config['stop_loss_usd']}usd"
    if config.get("stop_loss_pct") is not None:
        return f"stop_loss_{int(config['stop_loss_pct'] * 100)}pct"
    return "stop_loss"


def _take_profit_reason(config: dict) -> str:
    frac = config.get("take_profit_frac")
    if frac is not None:
        return f"take_profit_{int(frac * 100)}pct_of_move"
    return "take_profit"
