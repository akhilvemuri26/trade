"""Per-market model-score snapshot for the dashboard.

Once per entry scan we score every open moneyline market with the trained
model so the dashboard can show which markets Run 4 would bet vs skip. The
model only applies to underdog candidates inside the gate's price/time window;
favorites and out-of-window markets are listed but not scored (the model was
never trained on that range — see ml/ plan's underdog-only note).

The score for a given market varies slightly across strategies because each
strategy feeds its own exit-config features. We score under every gated
strategy and report the mean (headline) plus the min/max range and how many
strategies would bet, so the bet/skip picture is honest rather than a single
representative number.
"""
from __future__ import annotations

import json
import time

from paths import DATA_DIR
from strategy_config import MAX_HOURS_TO_RESOLUTION, STRATEGIES

SNAPSHOT_PATH = DATA_DIR / "market_scores.json"

GATE_ENABLED = any(c.get("use_model_gate") for c in STRATEGIES)


def _scoring_configs() -> list[dict]:
    gated = [c for c in STRATEGIES if c.get("use_model_gate")]
    return gated or list(STRATEGIES)


def _empty() -> dict:
    return {
        "scanned_at": None,
        "model_gate_enabled": GATE_ENABLED,
        "markets": [],
        "counts": {},
    }


def build_snapshot(markets: list) -> dict:
    from ml.features import build_features, features_to_row
    from ml.predict import _load

    configs = _scoring_configs()
    min_prob = float(configs[0].get("model_min_prob", 0.6)) if configs else 0.6
    model_path = configs[0].get("model_path") if configs else None
    underdog_max = (
        float(configs[0].get("underdog_max_price", 0.40)) if configs else 0.40
    )

    model = None
    model_error = None
    try:
        model = _load(model_path)
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI, never fatal
        model_error = str(exc)

    now = time.time()
    rows = []
    for m in markets:
        yes_ask = float(getattr(m, "yes_ask", 0.0) or 0.0)
        resolves_at = getattr(m, "resolves_at", None)
        hours = (resolves_at - now) / 3600 if resolves_at else None
        in_price = 0 < yes_ask <= underdog_max
        in_time = hours is not None and 0 < hours <= MAX_HOURS_TO_RESOLUTION
        in_scope = bool(in_price and in_time)

        prob_mean = prob_min = prob_max = None
        n_betting = 0
        if in_scope and model is not None:
            raw = {
                "entry_price": yes_ask,
                "sport": getattr(m, "sport", None),
                "ticker": getattr(m, "ticker", ""),
                "title": getattr(m, "title", ""),
                "timestamp": now,
            }
            probs = []
            for cfg in configs:
                feats = build_features(raw, cfg)
                p = float(model.predict_proba([features_to_row(feats)])[0, 1])
                probs.append(p)
                if p >= min_prob:
                    n_betting += 1
            prob_mean = sum(probs) / len(probs)
            prob_min = min(probs)
            prob_max = max(probs)

        if not in_scope:
            decision = "OUT_OF_SCOPE"
        elif model is None:
            decision = "NO_MODEL"
        elif n_betting > 0:
            decision = "BET"
        else:
            decision = "SKIP"

        rows.append({
            "ticker": getattr(m, "ticker", ""),
            "title": getattr(m, "title", ""),
            "sport": getattr(m, "sport", None),
            "yes_ask": round(yes_ask, 4),
            "no_ask": round(float(getattr(m, "no_ask", 0.0) or 0.0), 4),
            "volume": float(getattr(m, "volume", 0.0) or 0.0),
            "hours_to_resolution": round(hours, 2) if hours is not None else None,
            "in_scope": in_scope,
            "model_prob": round(prob_mean, 4) if prob_mean is not None else None,
            "model_prob_min": round(prob_min, 4) if prob_min is not None else None,
            "model_prob_max": round(prob_max, 4) if prob_max is not None else None,
            "strategies_total": len(configs),
            "strategies_betting": n_betting,
            "decision": decision,
        })

    order = {"BET": 0, "SKIP": 1, "NO_MODEL": 2, "OUT_OF_SCOPE": 3}
    rows.sort(key=lambda r: (order.get(r["decision"], 9), -(r["model_prob"] or 0)))

    return {
        "scanned_at": now,
        "model_gate_enabled": GATE_ENABLED,
        "model_min_prob": min_prob,
        "model_error": model_error,
        "markets": rows,
        "counts": {
            "total": len(rows),
            "bet": sum(1 for r in rows if r["decision"] == "BET"),
            "skip": sum(1 for r in rows if r["decision"] == "SKIP"),
            "out_of_scope": sum(1 for r in rows if r["decision"] == "OUT_OF_SCOPE"),
        },
    }


def write_snapshot(markets: list) -> None:
    try:
        snap = build_snapshot(markets)
        SNAPSHOT_PATH.write_text(json.dumps(snap, default=str))
    except Exception:  # noqa: BLE001 — snapshot is best-effort, never break scans
        pass


def read_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        return _empty()
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except Exception:  # noqa: BLE001
        return _empty()
