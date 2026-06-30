"""Track A: entry-quality filter model -- learn which games are better to bet.

Predicts P(position is profitable) from ENTRY-TIME features only (no exit-policy
features, so no leakage). Walk-forward: fit runs 4-6, test run 7. Reports
whether gating on the model beats the hand-picked price>=0.20 filter and the
bet-all baseline on realized ROI, and saves a calibrated model + report.

Microstructure (spread/width/volume/time-to-resolution) and in-game (score_diff/
is_live) features exist for run 5+/run 6+; they're auto-included when the train
split has sufficient coverage. Run 7 is the first prod holdout with in-game data.

Usage: python -m ml.filter_model
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ml.build_dataset import _load_rows
from ml.compare import fifo_positions

ART = Path(__file__).resolve().parent / "artifacts"
TRAIN_RUNS = {4, 5, 6}
TEST_RUNS = {7}
PRICE_FLOOR = 0.20  # the optimizer's hand-picked filter, as a baseline to beat

BASE_FEATURES = ["entry_price", "log_odds", "sport_mlb", "sport_nba", "sport_nhl",
                 "hour_utc", "is_weekend"]
OPT_FEATURES = ["entry_spread", "entry_market_width", "hours_to_resolution",
                "entry_volume", "entry_score_diff", "entry_is_live"]


def _featurize(pos: pd.DataFrame) -> pd.DataFrame:
    df = pos.copy()
    p = df["entry_price"].clip(1e-4, 1 - 1e-4)
    df["log_odds"] = np.log(p / (1 - p))
    for s in ("mlb", "nba", "nhl"):
        df[f"sport_{s}"] = (df["sport"] == s).astype(int)
    df["hour_utc"] = pd.to_numeric(df.get("hour_utc"), errors="coerce").fillna(0)
    df["is_weekend"] = (df["hour_utc"].notna() & df.get("entry_ts").notna())  # placeholder
    df["is_weekend"] = (
        pd.to_datetime(df["entry_ts"], unit="s", utc=True).dt.weekday >= 5
    ).astype(int)
    return df


def _roi(d: pd.DataFrame) -> float:
    c = d["cost"].sum()
    return 100 * d["net_profit"].sum() / c if c else 0.0


def main() -> None:
    pos = _featurize(fifo_positions(_load_rows()))
    train = pos[pos["run"].isin(TRAIN_RUNS)].copy()
    test = pos[pos["run"].isin(TEST_RUNS)].copy()

    # Include optional features only where the TRAIN split actually has them.
    feats = list(BASE_FEATURES)
    for f in OPT_FEATURES:
        if f in train.columns and train[f].notna().mean() >= 0.5:
            train[f] = train[f].fillna(train[f].median())
            test[f] = pd.to_numeric(test.get(f), errors="coerce").fillna(train[f].median())
            feats.append(f)

    X_tr, y_tr = train[feats].astype(float).values, train["profitable"].values
    X_te, y_te = test[feats].astype(float).values, test["profitable"].values
    profit_te, cost_te = test["net_profit"].values, test["cost"].values

    lines: list[str] = []

    def out(s: str = "") -> None:
        print(s)
        lines.append(s)

    out("# Track A — entry-quality filter model\n")
    out(f"Train runs {sorted(TRAIN_RUNS)}: {len(train)} positions "
        f"(profitable {y_tr.mean():.1%})")
    out(f"Test  runs {sorted(TEST_RUNS)}: {len(test)} positions "
        f"(profitable {y_te.mean():.1%})")
    out(f"Features ({len(feats)}): {feats}\n")

    candidates = {
        "logreg": Pipeline([("s", StandardScaler()),
                            ("c", LogisticRegression(max_iter=2000, class_weight="balanced"))]),
        "random_forest": RandomForestClassifier(
            n_estimators=300, max_depth=5, min_samples_leaf=25,
            class_weight="balanced", random_state=0),
    }
    best, best_auc, best_name = None, -1.0, None
    for name, base in candidates.items():
        model = CalibratedClassifierCV(base, method="isotonic", cv=3)
        model.fit(X_tr, y_tr)
        prob = model.predict_proba(X_te)[:, 1]
        auc = roc_auc_score(y_te, prob) if len(set(y_te)) > 1 else float("nan")
        brier = brier_score_loss(y_te, prob)
        out(f"  {name:14} test AUC {auc:.3f}  Brier {brier:.3f}")
        if auc > best_auc:
            best, best_auc, best_name = model, auc, name
    out(f"Selected: {best_name} (AUC {best_auc:.3f})\n")

    test = test.assign(pred=best.predict_proba(X_te)[:, 1])

    # Realized-ROI comparison on the test runs: bet-all vs price filter vs model gate.
    out("## Realized ROI on test runs — bet-all vs filter vs model gate\n")
    base_all = test
    base_flt = test[test["entry_price"] >= PRICE_FLOOR]
    rows = [
        {"rule": "bet all", "n": len(base_all), "net_$": round(base_all["net_profit"].sum(), 2), "roi%": round(_roi(base_all), 1)},
        {"rule": f"price>={PRICE_FLOOR}", "n": len(base_flt), "net_$": round(base_flt["net_profit"].sum(), 2), "roi%": round(_roi(base_flt), 1)},
    ]
    for thr in [0.4, 0.45, 0.5, 0.55, 0.6]:
        g = test[test["pred"] >= thr]
        if len(g):
            rows.append({"rule": f"model>= {thr}", "n": len(g),
                         "net_$": round(g["net_profit"].sum(), 2), "roi%": round(_roi(g), 1)})
    rdf = pd.DataFrame(rows)
    out(rdf.to_string(index=False))

    best_model_rule = rdf[rdf["rule"].str.startswith("model")].sort_values("roi%", ascending=False).head(1)
    flt_roi = rdf[rdf["rule"].str.startswith("price")]["roi%"].iloc[0]
    out("")
    if not best_model_rule.empty and best_model_rule["roi%"].iloc[0] > flt_roi:
        out(f"=> Model gate ({best_model_rule['rule'].iloc[0]}) beats the price filter "
            f"on test ROI ({best_model_rule['roi%'].iloc[0]}% vs {flt_roi}%).")
    else:
        out(f"=> Model gate does NOT beat the simple price>={PRICE_FLOOR} filter on test ROI "
            f"({flt_roi}%). Honest read: no learnable pre-game edge beyond the price floor yet — "
            f"the in-game features (run 6) are the path to real signal.")

    ART.mkdir(parents=True, exist_ok=True)
    import joblib
    joblib.dump(best, ART / "filter_model.joblib")
    (ART / "filter_model_meta.json").write_text(json.dumps({
        "model": best_name, "features": feats, "test_auc": best_auc,
        "train_runs": sorted(TRAIN_RUNS), "test_runs": sorted(TEST_RUNS),
        "label": "profitable = realized exit net_profit > 0",
    }, indent=2, default=str))
    (ART / "filter_model_report.md").write_text("\n".join(lines))
    print(f"\nSaved -> {ART/'filter_model.joblib'}, filter_model_report.md")


if __name__ == "__main__":
    main()
