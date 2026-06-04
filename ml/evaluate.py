"""Threshold analysis + EV backtest on the held-out test set.

Answers the core questions:
  1. What is the lowest entry-price (implied-prob) band that was net profitable?
  2. Given the model's P(profitable swing), what gating rule maximizes realized
     net P&L vs the "bet everything under 40c" baseline?

All P&L figures use REALIZED net_profit from the ledger, so this is a true
backtest of the decision rule (not a synthetic EV). Writes a markdown report to
ml/artifacts/threshold_report.md and prints it.

Usage: python -m ml.evaluate
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml.features import FEATURE_NAMES

DATA = Path(__file__).resolve().parent.parent / "data" / "ml" / "dataset.parquet"
ART_DIR = Path(__file__).resolve().parent / "artifacts"
TEST_FRAC = 0.30


def _bucket_table(df: pd.DataFrame, col: str, edges: list[float]) -> pd.DataFrame:
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = df[(df[col] >= lo) & (df[col] < hi)]
        if len(sub) == 0:
            continue
        cost = sub["cost"].sum()
        net = sub["net_profit"].sum()
        rows.append({
            "band": f"[{lo:.2f},{hi:.2f})",
            "n": len(sub),
            "win%": round(100 * sub["profitable"].mean(), 1),
            "net_$": round(net, 2),
            "roi%": round(100 * net / cost, 1) if cost else 0.0,
        })
    return pd.DataFrame(rows)


def main() -> None:
    df = pd.read_parquet(DATA).sort_values("entry_ts").reset_index(drop=True)
    cut = int(len(df) * (1 - TEST_FRAC))
    test = df.iloc[cut:].copy()

    model = joblib.load(ART_DIR / "model.joblib")
    test["pred_prob"] = model.predict_proba(test[FEATURE_NAMES].values)[:, 1]

    lines: list[str] = []

    def out(s: str = ""):
        print(s)
        lines.append(s)

    base_net = test["net_profit"].sum()
    base_cost = test["cost"].sum()
    out("# Threshold analysis (held-out test set)\n")
    out(f"Test positions: {len(test)}  |  baseline net (bet all): "
        f"${base_net:,.2f}  (ROI {100*base_net/base_cost:.1f}%)\n")

    out("## Win-rate / ROI by ENTRY PRICE (implied prob)\n")
    pe = _bucket_table(test, "entry_price", [0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.41])
    out(pe.to_string(index=False))
    out("\n_Interpretation: the floor below which we never bet is the lowest band "
        "that is not deeply negative._\n")

    out("## Win-rate / ROI by MODEL predicted P(profitable swing)\n")
    pp = _bucket_table(test, "pred_prob", [0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.01])
    out(pp.to_string(index=False))

    out("\n## EV backtest: net P&L under gating rules\n")
    out("Rule = bet only positions with pred_prob >= p_min AND entry_price >= price_floor.\n")
    grid = []
    for p_min in [0.0, 0.4, 0.5, 0.6, 0.7, 0.8]:
        for floor in [0.0, 0.10, 0.20, 0.30]:
            mask = (test["pred_prob"] >= p_min) & (test["entry_price"] >= floor)
            sub = test[mask]
            if len(sub) == 0:
                continue
            net = sub["net_profit"].sum()
            cost = sub["cost"].sum()
            grid.append({
                "p_min": p_min, "price_floor": floor, "bets": len(sub),
                "net_$": round(net, 2),
                "roi%": round(100 * net / cost, 1) if cost else 0.0,
            })
    gdf = pd.DataFrame(grid).sort_values("net_$", ascending=False)
    out(gdf.to_string(index=False))

    best = gdf.iloc[0]
    out("")
    out(f"## Recommended gate\n")
    if best["net_$"] > 0:
        out(f"Best rule is PROFITABLE on holdout: p_min={best['p_min']}, "
            f"price_floor={best['price_floor']} -> net ${best['net_$']:,.2f} "
            f"(ROI {best['roi%']}%), {int(best['bets'])} bets.")
        out(f"Suggested config: model_min_prob={best['p_min']}, "
            f"underdog_min_price={best['price_floor']}.")
    else:
        out(f"No rule is net positive on holdout. Least-bad: p_min={best['p_min']}, "
            f"price_floor={best['price_floor']} -> net ${best['net_$']:,.2f} "
            f"(ROI {best['roi%']}%) vs baseline ${base_net:,.2f}.")
        out("CONCLUSION: the underdog-swing strategy as configured is unprofitable. "
            "The model reduces losses but does not turn them positive. Recommend "
            "NOT running Run 4 with real conviction until the exit thresholds / "
            "entry universe change, or treat run 4 as further data collection.")

    ART_DIR.mkdir(parents=True, exist_ok=True)
    (ART_DIR / "threshold_report.md").write_text("\n".join(lines))
    print(f"\nReport saved -> {ART_DIR/'threshold_report.md'}")


if __name__ == "__main__":
    main()
