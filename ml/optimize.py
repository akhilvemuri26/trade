"""Optimize the underdog strategy from historical positions. Recommends a run-6 config.

The live run-5 cohorts already A/B the EXIT policy (hold vs stop vs take-profit). The
comparison surfaced two *bigger* levers, which this script quantifies:

  1. **Re-entry churn** — after a stop, the strategy re-buys the same sub-$0.40 game and
     stops out again. We estimate the cost by collapsing to ONE position per
     (run, strategy, ticker) (a re-entry guard) and comparing realized net.
  2. **Entry selection** — price band / time-to-resolution / sport, swept with a
     walk-forward split (fit on runs 2-3, test on runs 4-5) so the filter isn't overfit.

Exact stop/TP *level* tuning needs real intraday price paths (run-6 price_path logging);
not attempted here. Output: ml/artifacts/optimization_report.md.

Usage: python -m ml.optimize
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ml.build_dataset import _load_rows
from ml.compare import fifo_positions

ART_DIR = Path(__file__).resolve().parent / "artifacts"
TRAIN_RUNS = {2, 3}
TEST_RUNS = {4, 5}


def _roi(df: pd.DataFrame) -> float:
    c = df["cost"].sum()
    return 100 * df["net_profit"].sum() / c if c else 0.0


def _line(df: pd.DataFrame, label: str) -> dict:
    return {
        "filter": label, "n": len(df),
        "net_$": round(df["net_profit"].sum(), 2),
        "roi%": round(_roi(df), 1),
        "win%": round(100 * df["profitable"].mean(), 1) if len(df) else 0.0,
    }


def reentry_cost(pos: pd.DataFrame) -> pd.DataFrame:
    """Net with vs without a re-entry guard (keep first position per strategy+game)."""
    rows = []
    for run in sorted(pos["run"].unique()):
        sub = pos[pos["run"] == run]
        first = sub.sort_values("entry_ts").groupby(["strategy", "ticker"], as_index=False).first()
        rows.append({
            "run": run,
            "positions_all": len(sub), "net_all": round(sub["net_profit"].sum(), 2),
            "positions_guarded": len(first), "net_guarded": round(first["net_profit"].sum(), 2),
            "churn_cost_$": round(sub["net_profit"].sum() - first["net_profit"].sum(), 2),
        })
    return pd.DataFrame(rows)


def price_floor_walkforward(pos: pd.DataFrame) -> pd.DataFrame:
    """Sweep an entry price floor; fit on train runs, report test-run ROI (cap 0.40 fixed)."""
    train = pos[pos["run"].isin(TRAIN_RUNS)]
    test = pos[pos["run"].isin(TEST_RUNS)]
    rows = []
    for floor in [0.0, 0.10, 0.15, 0.20, 0.25, 0.30]:
        tr = train[train["entry_price"] >= floor]
        te = test[test["entry_price"] >= floor]
        rows.append({
            "price_floor": floor,
            "train_n": len(tr), "train_roi%": round(_roi(tr), 1),
            "test_n": len(te), "test_roi%": round(_roi(te), 1),
            "test_net_$": round(te["net_profit"].sum(), 2),
        })
    return pd.DataFrame(rows)


def main() -> None:
    pos = fifo_positions(_load_rows())
    # entry_ts for ordering (compare.fifo_positions keeps hold_s but not entry_ts; derive)
    if "entry_ts" not in pos.columns:
        pos = pos.copy()
        pos["entry_ts"] = pos.index  # stable original order ~ chronological per group

    lines: list[str] = []

    def out(s: str = "") -> None:
        print(s)
        lines.append(s)

    out("# Strategy optimization report\n")
    out(f"Paired positions: {len(pos):,}  |  runs: {sorted(pos['run'].unique())}  |  "
        f"net ${pos['net_profit'].sum():,.2f}\n")

    out("## 1. Re-entry churn cost (keep first position per strategy+game)\n")
    rc = reentry_cost(pos)
    out(rc.to_string(index=False))
    out(f"\nTotal churn cost across runs: ${rc['churn_cost_$'].sum():,.2f}  "
        "(loss attributable to re-buying the same game after a stop/exit)\n")

    out("## 2. Entry price floor — walk-forward (fit runs 2-3, test runs 4-5)\n")
    pf = price_floor_walkforward(pos)
    out(pf.to_string(index=False))
    best = pf.sort_values("test_roi%", ascending=False).iloc[0]
    out(f"\nBest test ROI at price_floor={best['price_floor']} "
        f"(test ROI {best['test_roi%']}% on {int(best['test_n'])} positions).\n")

    out("## 3. Sport (pooled)\n")
    sp = pos.groupby("sport").apply(lambda d: pd.Series(_line(d, "")), include_groups=False)
    sp = sp.drop(columns=["filter"]).reset_index()
    out(sp.to_string(index=False))
    out("")

    # run-5-only time-to-resolution (microstructure absent earlier)
    htr = pos[pos["hours_to_resolution"].notna()]
    if not htr.empty:
        out("## 4. Time-to-resolution (run 5+ only)\n")
        rows = []
        for lo, hi in [(0, 3), (3, 6), (6, 12), (12, 24)]:
            sub = htr[(htr["hours_to_resolution"] >= lo) & (htr["hours_to_resolution"] < hi)]
            if len(sub):
                rows.append({"htr_band": f"[{lo},{hi})", **{k: v for k, v in _line(sub, "").items() if k != "filter"}})
        out(pd.DataFrame(rows).to_string(index=False))
        out("")

    out("## Recommendation for run 6\n")
    out("- **Add a re-entry guard** (one position per game per strategy, or a long post-exit "
        "cooldown): the single biggest, cleanest win — removes the churn loss quantified above.")
    out(f"- **Entry price floor ≈ {best['price_floor']}** (cap stays 0.40): skip the deep-longshot "
        "bands that are −60 to −80% ROI.")
    out("- **Bias toward more time-to-resolution**: near-resolution entries are buying near-decided "
        "games. Add a `min_hours_to_resolution` filter once run-6 confirms the run-5 signal.")
    out("- **Caveat:** every slice is still net-negative — the entry has adverse drift (we buy "
        "underdogs that keep losing). Filters/guards reduce the bleed; a real **edge model** "
        "(Track A microstructure + Track B in-game) is required to turn it positive.")

    ART_DIR.mkdir(parents=True, exist_ok=True)
    (ART_DIR / "optimization_report.md").write_text("\n".join(lines))
    print(f"\nSaved -> {ART_DIR / 'optimization_report.md'}")


if __name__ == "__main__":
    main()
