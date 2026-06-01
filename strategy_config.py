# Paper sim — underdog swing exit experiment
import os

_MAX_RISK = float(os.getenv("MAX_RISK_PER_TRADE", "25"))
_PROFIT_SCALE = float(os.getenv("PROFIT_SCALE", "1.0"))

MAX_TOTAL_RISK = int(os.getenv("MAX_TOTAL_RISK", "50000"))
MAX_HOURS_TO_RESOLUTION = 24

# Game-winner moneylines only: KXNBAGAME, KXMLBGAME, KXNHLGAME (kalshi_client).

_EXPERIMENT_BASE = {
    "moneyline_only": True,
    "underdog_max_price": 0.40,
    "max_risk": _MAX_RISK,
    "max_positions": 200,
    "min_volume": 0,
    "enter_all_markets": True,
    "ignore_global_risk_cap": True,
    "use_run_exits": False,
    "sports": ["nba", "mlb", "nhl"],
}


def _sell_usd(amount):
    return {
        **_EXPERIMENT_BASE,
        "name": f"swing_sell_{int(amount)}usd",
        "exit_mode": "sell",
        "min_sell_profit_usd": round(amount * _PROFIT_SCALE, 2),
    }


def _sell_pct(pct):
    return {
        **_EXPERIMENT_BASE,
        "name": f"swing_sell_{int(pct * 100)}pct",
        "exit_mode": "sell",
        "min_sell_profit_pct": pct,
    }


def _lock_pct(pct):
    return {
        **_EXPERIMENT_BASE,
        "name": f"swing_lock_{int(pct * 100)}pct",
        "exit_mode": "lock",
        "min_lock_profit_pct": pct,
    }


def _lock_usd(amount):
    return {
        **_EXPERIMENT_BASE,
        "name": f"swing_lock_{int(amount)}usd",
        "exit_mode": "lock",
        "min_lock_profit_usd": round(amount * _PROFIT_SCALE, 2),
    }


_SELL_USD_AMOUNTS = [1, 2, 3, 5, 7, 8, 10, 12, 15, 18, 20, 25, 30]
_SELL_PCT_VALUES = [0.02, 0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
_LOCK_PCT_VALUES = [0.02, 0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
_LOCK_USD_AMOUNTS = [1, 2, 3, 5, 7, 8, 10, 12, 15, 18, 20, 25, 30]

STRATEGIES = (
    [_sell_usd(a) for a in _SELL_USD_AMOUNTS]
    + [_sell_pct(p) for p in _SELL_PCT_VALUES]
    + [_lock_pct(p) for p in _LOCK_PCT_VALUES]
    + [_lock_usd(a) for a in _LOCK_USD_AMOUNTS]
)
