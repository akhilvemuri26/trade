"""Build the training dataset from raw run ledgers.

One row per CLOSED position, keyed by (run, strategy, ticker):
  - join the `buy` (entry) to its terminal exit (sell|lock|settle),
  - label `profitable = 1` if the terminal exit profit > 0 (a favorable
    sell/lock swing) else 0 (held to a losing settlement),
  - keep `net_profit`, `cost`, `roi` for the EV backtest / threshold analysis.

Why position-level and not game-outcome: only 76 games ever settled and ~1 was
a win, because the strategies exit winners early. The real signal is whether an
entry produced a profitable swing exit. See plan for details.

Output: data/ml/dataset.parquet (+ .csv fallback).
Usage: python -m ml.build_dataset
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

from ml.features import build_features

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "ml" / "raw"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "ml"

_BASE_CFG = {"underdog_max_price": 0.40, "max_risk": 25.0}


def _strategy_map() -> dict:
    try:
        import strategy_config as sc
        return {c["name"]: c for c in sc.STRATEGIES}
    except Exception:
        return {}


def _parse_strategy_name(name: str) -> dict:
    """Fallback config for strategy names not in strategy_config.STRATEGIES."""
    cfg = dict(_BASE_CFG)
    cfg["name"] = name
    if name.startswith("swing_sell_"):
        cfg["exit_mode"] = "sell"
        m = re.search(r"_(\d+)(usd|pct)$", name)
        if m and m.group(2) == "usd":
            cfg["min_sell_profit_usd"] = float(m.group(1))
        elif m:
            cfg["min_sell_profit_pct"] = float(m.group(1)) / 100.0
    elif name.startswith("swing_lock_"):
        cfg["exit_mode"] = "lock"
        m = re.search(r"_(\d+)(usd|pct)$", name)
        if m and m.group(2) == "usd":
            cfg["min_lock_profit_usd"] = float(m.group(1))
        elif m:
            cfg["min_lock_profit_pct"] = float(m.group(1)) / 100.0
        # swing_lock_any -> threshold 0 (already default)
    elif name.startswith("swing_hybrid_"):
        cfg["exit_mode"] = "hybrid"
        for amt, kind in re.findall(r"sell(\d+)|lock(\d+)", name):
            if amt:
                cfg["min_sell_profit_usd"] = float(amt)
            if kind:
                cfg["min_lock_profit_usd"] = float(kind)
    return cfg


def _resolve_cfg(name: str, smap: dict) -> dict:
    return smap.get(name) or _parse_strategy_name(name or "")


def _load_rows() -> list[dict]:
    rows = []
    for path in sorted(RAW_DIR.glob("run*_trades.jsonl")):
        run = int(re.search(r"run(\d+)_", path.name).group(1))
        for line in path.open():
            r = json.loads(line)
            r["_run"] = run
            rows.append(r)
    return rows


def build() -> pd.DataFrame:
    rows = _load_rows()
    smap = _strategy_map()

    positions: dict[tuple, dict] = defaultdict(lambda: {"buy": None, "exit": None})
    for r in rows:
        key = (r["_run"], r.get("strategy"), r.get("ticker"))
        action = r.get("action")
        if action == "buy":
            positions[key]["buy"] = r
        elif action in ("sell", "lock", "settle"):
            cur = positions[key]["exit"]
            if cur is None or (r.get("timestamp") or 0) > (cur.get("timestamp") or 0):
                positions[key]["exit"] = r

    records = []
    for (run, strat, ticker), v in positions.items():
        buy, exit_ = v["buy"], v["exit"]
        if not buy or not exit_:
            continue  # skip still-open positions
        cfg = _resolve_cfg(strat, smap)
        entry_price = float(buy.get("price") or buy.get("entry_price") or 0.0)
        count = int(buy.get("count") or 0)
        cost = float(buy.get("cost") or entry_price * count)
        profit = float(exit_.get("profit") or 0.0)

        raw = {
            "entry_price": entry_price,
            "sport": buy.get("sport"),
            "ticker": ticker,
            "title": buy.get("title"),
            "timestamp": buy.get("timestamp"),
        }
        feats = build_features(raw, cfg)
        rec = dict(feats)
        rec.update({
            "run": run,
            "strategy": strat,
            "ticker": ticker,
            "exit_action": exit_.get("action"),
            "count": count,
            "cost": cost,
            "net_profit": profit,
            "roi": (profit / cost) if cost else 0.0,
            "profitable": 1 if profit > 0 else 0,
            "entry_ts": float(buy.get("timestamp") or 0.0),
        })
        records.append(rec)

    df = pd.DataFrame(records).sort_values("entry_ts").reset_index(drop=True)
    return df


def main() -> None:
    df = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pq = OUT_DIR / "dataset.parquet"
    df.to_parquet(pq, index=False)
    df.to_csv(OUT_DIR / "dataset.csv", index=False)

    n = len(df)
    pos = int(df["profitable"].sum())
    print(f"Wrote {n} closed positions -> {pq}")
    print(f"  profitable={pos} ({100*pos/n:.1f}%)  unprofitable={n-pos}")
    print(f"  net P&L across all positions: ${df['net_profit'].sum():,.2f}")
    print(f"  exit mix: {df['exit_action'].value_counts().to_dict()}")
    print(f"  runs: {df['run'].value_counts().to_dict()}")
    print(f"  features: {len(df.columns)} cols")


if __name__ == "__main__":
    main()
