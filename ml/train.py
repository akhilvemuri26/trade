"""Train and compare calibrated classifiers for P(profitable swing exit).

Compares Logistic Regression, Random Forest, Gradient Boosting, and XGBoost
(if installed), each wrapped in calibration. Uses a TIME-BASED split (train on
earlier positions, test on later) to avoid look-ahead leakage. Selection is by
test ROC-AUC with Brier score as tie-break; calibration matters because we
compare predicted prob to the market's implied price downstream.

Persists the best calibrated model + metadata to ml/artifacts/.
Usage: python -m ml.train
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ml.features import FEATURE_NAMES

warnings.filterwarnings("ignore")

DATA = Path(__file__).resolve().parent.parent / "data" / "ml" / "dataset.parquet"
ART_DIR = Path(__file__).resolve().parent / "artifacts"
TEST_FRAC = 0.30


def _candidates() -> dict:
    models = {
        "logreg": Pipeline([
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
        ]),
        "random_forest": RandomForestClassifier(
            n_estimators=300, max_depth=6, min_samples_leaf=20,
            class_weight="balanced", random_state=0,
        ),
        "grad_boost": GradientBoostingClassifier(
            n_estimators=200, max_depth=3, learning_rate=0.05, random_state=0,
        ),
    }
    try:
        from xgboost import XGBClassifier
        models["xgboost"] = XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, eval_metric="logloss",
            random_state=0,
        )
    except Exception as e:  # pragma: no cover
        print(f"  (xgboost unavailable: {e})")
    return models


def _ev_at_threshold(prob: np.ndarray, net_profit: np.ndarray, p_min: float) -> tuple[int, float]:
    mask = prob >= p_min
    return int(mask.sum()), float(net_profit[mask].sum())


def main() -> None:
    df = pd.read_parquet(DATA).sort_values("entry_ts").reset_index(drop=True)
    n = len(df)
    cut = int(n * (1 - TEST_FRAC))
    train, test = df.iloc[:cut], df.iloc[cut:]

    X_tr, y_tr = train[FEATURE_NAMES].values, train["profitable"].values
    X_te, y_te = test[FEATURE_NAMES].values, test["profitable"].values
    profit_te = test["net_profit"].values

    print(f"Dataset: {n} positions | train={len(train)} test={len(test)} (time-split)")
    print(f"  train profitable={y_tr.mean():.1%}  test profitable={y_te.mean():.1%}")
    print(f"  baseline (bet everything in test): net ${profit_te.sum():,.2f}\n")

    results = []
    fitted = {}
    for name, base in _candidates().items():
        # Calibrate with internal CV on the training set only.
        model = CalibratedClassifierCV(base, method="isotonic", cv=3)
        model.fit(X_tr, y_tr)
        prob = model.predict_proba(X_te)[:, 1]
        auc = roc_auc_score(y_te, prob) if len(set(y_te)) > 1 else float("nan")
        brier = brier_score_loss(y_te, prob)
        ll = log_loss(y_te, prob, labels=[0, 1])
        # Quick EV at a representative threshold (full sweep lives in evaluate.py).
        n_bet, ev = _ev_at_threshold(prob, profit_te, 0.5)
        results.append({
            "model": name, "roc_auc": auc, "brier": brier, "log_loss": ll,
            "bets@0.5": n_bet, "net@0.5": ev,
        })
        fitted[name] = model

    res = pd.DataFrame(results).sort_values(["roc_auc", "brier"], ascending=[False, True])
    pd.set_option("display.float_format", lambda v: f"{v:.4f}")
    print("Model comparison (test set):")
    print(res.to_string(index=False))

    best_name = res.iloc[0]["model"]
    best = fitted[best_name]
    print(f"\nSelected: {best_name} (highest ROC-AUC, Brier tie-break)")

    ART_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(best, ART_DIR / "model.joblib")
    meta = {
        "model_name": best_name,
        "feature_names": FEATURE_NAMES,
        "trained_on_runs": sorted(int(r) for r in df["run"].unique()),
        "n_train": len(train), "n_test": len(test),
        "test_metrics": res[res["model"] == best_name].iloc[0].to_dict(),
        "label": "profitable = terminal exit profit > 0 (favorable swing)",
        "note": "Predicts P(profitable swing). Combine with EV backtest before betting.",
    }
    (ART_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    print(f"Saved -> {ART_DIR/'model.joblib'} and metadata.json")


if __name__ == "__main__":
    main()
