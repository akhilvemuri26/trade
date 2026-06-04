"""Live scoring for Run 4: P(profitable swing exit) for a candidate entry.

Loads the trained model once (lazy singleton) and scores a Market using the
SAME feature pipeline as training (ml.features), guaranteeing live/train parity.

Used by virtual_trader.py when a strategy config sets use_model_gate=True.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ml.features import FEATURE_NAMES, build_features, features_to_row

ART_DIR = Path(__file__).resolve().parent / "artifacts"
DEFAULT_MODEL_PATH = ART_DIR / "model.joblib"

_model_cache: dict[str, Any] = {}


def _load(model_path: str | None = None):
    path = str(model_path or DEFAULT_MODEL_PATH)
    if path not in _model_cache:
        import joblib  # local import so non-Run-4 code paths don't need it
        _model_cache[path] = joblib.load(path)
    return _model_cache[path]


def score(market: Any, strat_cfg: dict, model_path: str | None = None) -> float:
    """Return P(profitable swing) in [0,1] for a live Market under strat_cfg.

    `market` is a models.Market (has .ticker, .title, .yes_ask, .sport,
    .resolves_at). Entry price = yes_ask (the price we'd pay).
    """
    raw = {
        "entry_price": getattr(market, "yes_ask", 0.0),
        "sport": getattr(market, "sport", None),
        "ticker": getattr(market, "ticker", ""),
        "title": getattr(market, "title", ""),
        "timestamp": __import__("time").time(),
    }
    feats = build_features(raw, strat_cfg)
    model = _load(model_path)
    row = [features_to_row(feats)]
    return float(model.predict_proba(row)[0, 1])


def should_bet(prob: float, strat_cfg: dict) -> bool:
    return prob >= float(strat_cfg.get("model_min_prob", 0.0))
