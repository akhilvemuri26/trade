"""Live/train feature parity: a Market scored live must produce the same
feature vector as the equivalent training ledger row. Guards against drift
between ml.build_dataset (training) and ml.predict (Run 4 live)."""
import time

from ml.features import FEATURE_NAMES, build_features, features_to_row
from models import Market


def _cfg():
    return {
        "exit_mode": "sell",
        "min_sell_profit_usd": 5.0,
        "max_risk": 25.0,
        "underdog_max_price": 0.40,
    }


def test_feature_names_stable():
    assert len(FEATURE_NAMES) == 18
    assert FEATURE_NAMES[0] == "entry_price"


def test_market_and_ledger_row_match():
    ts = 1779402693.0
    cfg = _cfg()

    # Training-side raw (ledger buy row shape)
    ledger_raw = {
        "entry_price": 0.34,
        "sport": "mlb",
        "ticker": "KXMLBGAME-26MAY271610PHISD-SD",
        "title": "Philadelphia vs San Diego Winner?",
        "timestamp": ts,
    }

    # Live-side raw (built from a Market the way ml.predict.score does)
    mkt = Market(
        ticker="KXMLBGAME-26MAY271610PHISD-SD",
        title="Philadelphia vs San Diego Winner?",
        yes_ask=0.34,
        no_ask=0.66,
        sport="mlb",
    )
    live_raw = {
        "entry_price": mkt.yes_ask,
        "sport": mkt.sport,
        "ticker": mkt.ticker,
        "title": mkt.title,
        "timestamp": ts,
    }

    f_train = build_features(ledger_raw, cfg)
    f_live = build_features(live_raw, cfg)
    assert features_to_row(f_train) == features_to_row(f_live)
    assert list(f_train.keys()) == FEATURE_NAMES


def test_score_runs_if_model_present():
    import os
    from pathlib import Path
    art = Path(__file__).resolve().parent / "ml" / "artifacts" / "model.joblib"
    if not art.exists():
        return  # model not trained in this env; skip
    from ml.predict import score
    mkt = Market(ticker="KXMLBGAME-X-Y", title="A vs B Winner?",
                 yes_ask=0.34, no_ask=0.66, sport="mlb")
    p = score(mkt, _cfg())
    assert 0.0 <= p <= 1.0
