"""Shared feature engineering for training (ledger rows) and live scoring (Run 4).

The model predicts P(favorable swing exit) for an underdog entry: i.e. whether
the position will be sold/locked at a profit before being held to a losing
settlement. Profit in this strategy comes from the upward price swing, so the
features describe the entry conditions plus the strategy's exit aggressiveness.

`build_features` accepts a plain dict so it works identically from:
  - a training position (entry ledger row + strategy config), and
  - a live candidate (Market fields + the active strategy config).

Keep this the single source of truth for feature names/order so live and train
vectors are guaranteed identical (verified by test_ml_features.py).
"""
from __future__ import annotations

import datetime

SPORTS = ("mlb", "nba", "nhl")
MONEYLINE_PREFIXES = ("KXMLBGAME", "KXNBAGAME", "KXNHLGAME")

# Ordered feature names. predict + train both rely on this order.
FEATURE_NAMES = [
    "entry_price",
    "implied_prob_log_odds",
    "sport_mlb",
    "sport_nba",
    "sport_nhl",
    "is_moneyline",
    "n_legs",
    "hour_of_day",
    "day_of_week",
    "is_weekend",
    "exit_is_sell",
    "exit_is_lock",
    "sell_thresh_usd",
    "sell_thresh_pct",
    "lock_thresh_usd",
    "lock_thresh_pct",
    "max_risk",
    "underdog_max_price",
]


def _safe_log_odds(p: float) -> float:
    import math
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def build_features(raw: dict, strat_cfg: dict) -> dict:
    """Return an ordered dict of features.

    raw expects: entry_price (float, the YES ask at entry), sport (str),
        ticker (str), title (str), timestamp (unix float).
    strat_cfg expects the strategy config dict (exit_mode, min_*_profit_*,
        max_risk, underdog_max_price).
    """
    entry_price = float(raw.get("entry_price") or raw.get("price") or 0.0)
    sport = (raw.get("sport") or "").lower()
    ticker = raw.get("ticker") or ""
    title = raw.get("title") or ""
    ts = float(raw.get("timestamp") or 0.0)

    prefix = ticker.split("-")[0] if ticker else ""
    is_moneyline = 1 if prefix in MONEYLINE_PREFIXES else 0
    # Parlay/multi-leg titles join legs with commas; moneylines are single-leg.
    n_legs = title.count(",") + 1 if title else 1

    if ts:
        dt = datetime.datetime.fromtimestamp(ts)
        hour, dow = dt.hour, dt.weekday()
    else:
        hour, dow = 0, 0

    exit_mode = strat_cfg.get("exit_mode", "")
    feats = {
        "entry_price": entry_price,
        "implied_prob_log_odds": _safe_log_odds(entry_price),
        "sport_mlb": 1 if sport == "mlb" else 0,
        "sport_nba": 1 if sport == "nba" else 0,
        "sport_nhl": 1 if sport == "nhl" else 0,
        "is_moneyline": is_moneyline,
        "n_legs": n_legs,
        "hour_of_day": hour,
        "day_of_week": dow,
        "is_weekend": 1 if dow >= 5 else 0,
        "exit_is_sell": 1 if exit_mode == "sell" else 0,
        "exit_is_lock": 1 if exit_mode == "lock" else 0,
        "sell_thresh_usd": float(strat_cfg.get("min_sell_profit_usd") or 0.0),
        "sell_thresh_pct": float(strat_cfg.get("min_sell_profit_pct") or 0.0),
        "lock_thresh_usd": float(strat_cfg.get("min_lock_profit_usd") or 0.0),
        "lock_thresh_pct": float(strat_cfg.get("min_lock_profit_pct") or 0.0),
        "max_risk": float(strat_cfg.get("max_risk") or 0.0),
        "underdog_max_price": float(strat_cfg.get("underdog_max_price") or 0.0),
    }
    return {k: feats[k] for k in FEATURE_NAMES}


def features_to_row(feats: dict) -> list[float]:
    return [feats[k] for k in FEATURE_NAMES]
