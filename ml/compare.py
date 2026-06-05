"""Compare exit cohorts across runs on the same games.

Two views:
  1. **Per-cohort P&L** (exact): aggregated straight from the raw rows -- realized
     net is the sum of `profit` over exit rows, cost basis the sum over buy rows.
     This is immune to any buy->exit pairing assumption (re-entries on the same
     ticker are common and would corrupt a key-collapsed join like build_dataset's).
  2. **Entry-feature breakdowns**: FIFO-pair each buy to its next exit within
     (run, strategy, ticker), then bucket by entry price / time-to-resolution /
     spread / sport. Microstructure fields (entry_spread, hours_to_resolution, ...)
     exist only for run 5+ (logged at virtual_trader entry); rows without them are
     dropped from those specific breakdowns.

Run 5's 7 cohorts (hold_* / tp_*) all bet the same games, so their per-cohort table
*is* a paired comparison. Output: ml/artifacts/cohort_report.md + data/ml/cohort_summary.csv.

Usage: python -m ml.compare            (all local runs)
       python -m ml.compare --run 5    (single run)
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd

from ml.build_dataset import _load_rows

ART_DIR = Path(__file__).resolve().parent / "artifacts"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "ml"
EXIT_ACTIONS = ("sell", "lock", "settle")

# Passthrough entry-state fields logged on run-5+ buy rows (absent earlier).
MICRO_FIELDS = (
    "entry_spread", "entry_market_width", "hours_to_resolution",
    "entry_yes_ask", "entry_no_ask", "entry_volume",
    # in-game (run 6+): best-effort live score state at entry
    "entry_score_diff", "entry_is_live", "entry_seconds_since_score",
)


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# View 1: exact per-cohort P&L straight from raw rows
# --------------------------------------------------------------------------- #
def cohort_summary(rows: list[dict]) -> pd.DataFrame:
    agg: dict[tuple, dict] = defaultdict(lambda: {
        "buys": 0, "cost": 0.0, "exits": 0, "net": 0.0, "wins": 0,
        "win_profit": 0.0, "loss_profit": 0.0,
        "settle": 0, "sell": 0, "lock": 0,
    })
    for r in rows:
        key = (r.get("_run"), r.get("strategy"))
        a = r.get("action")
        d = agg[key]
        if a == "buy":
            d["buys"] += 1
            d["cost"] += _f(r.get("cost") or _f(r.get("price")) * _f(r.get("count")))
        elif a in EXIT_ACTIONS:
            p = _f(r.get("profit"))
            d["exits"] += 1
            d["net"] += p
            d[a] += 1
            if p > 0:
                d["wins"] += 1
                d["win_profit"] += p
            else:
                d["loss_profit"] += p

    recs = []
    for (run, strat), d in agg.items():
        exits = d["exits"] or 1
        losses = d["exits"] - d["wins"] or 1
        recs.append({
            "run": run, "strategy": strat,
            "buys": d["buys"], "exits": d["exits"],
            "win%": round(100 * d["wins"] / exits, 1),
            "net_$": round(d["net"], 2),
            "roi%": round(100 * d["net"] / d["cost"], 1) if d["cost"] else 0.0,
            "avg_win": round(d["win_profit"] / d["wins"], 2) if d["wins"] else 0.0,
            "avg_loss": round(d["loss_profit"] / losses, 2),
            "wl_ratio": round((d["win_profit"] / d["wins"]) / abs(d["loss_profit"] / losses), 2)
            if d["wins"] and d["loss_profit"] else 0.0,
            "settle%": round(100 * d["settle"] / exits, 1),
        })
    return pd.DataFrame(recs).sort_values(["run", "net_$"], ascending=[True, False])


# --------------------------------------------------------------------------- #
# View 2: FIFO-paired positions for entry-feature breakdowns
# --------------------------------------------------------------------------- #
def fifo_positions(rows: list[dict]) -> pd.DataFrame:
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r.get("_run"), r.get("strategy"), r.get("ticker"))].append(r)

    recs = []
    for (run, strat, ticker), rs in by_key.items():
        rs.sort(key=lambda x: _f(x.get("timestamp")))
        open_buys: deque[dict] = deque()
        for r in rs:
            a = r.get("action")
            if a == "buy":
                open_buys.append(r)
            elif a in EXIT_ACTIONS and open_buys:
                buy = open_buys.popleft()
                entry = _f(buy.get("price") or buy.get("entry_price"))
                count = int(_f(buy.get("count")))
                cost = _f(buy.get("cost") or entry * count)
                profit = _f(r.get("profit"))
                rec = {
                    "run": run, "strategy": strat, "ticker": ticker,
                    "sport": (buy.get("sport") or "").lower(),
                    "entry_ts": _f(buy.get("timestamp")),
                    "entry_price": entry, "count": count, "cost": cost,
                    "exit_action": a, "net_profit": profit,
                    "roi": profit / cost if cost else 0.0,
                    "profitable": 1 if profit > 0 else 0,
                    "hold_s": _f(r.get("timestamp")) - _f(buy.get("timestamp")),
                    "hour_utc": pd.to_datetime(_f(buy.get("timestamp")), unit="s", utc=True).hour
                    if _f(buy.get("timestamp")) else None,
                }
                for k in MICRO_FIELDS:
                    rec[k] = _f(buy.get(k), default=float("nan")) if buy.get(k) is not None else float("nan")
                recs.append(rec)
    return pd.DataFrame(recs)


def _bucket(df: pd.DataFrame, col: str, edges: list[float]) -> pd.DataFrame:
    sub_all = df[df[col].notna()]
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = sub_all[(sub_all[col] >= lo) & (sub_all[col] < hi)]
        if len(sub) == 0:
            continue
        cost = sub["cost"].sum()
        net = sub["net_profit"].sum()
        rows.append({
            "band": f"[{lo:g},{hi:g})", "n": len(sub),
            "win%": round(100 * sub["profitable"].mean(), 1),
            "net_$": round(net, 2),
            "roi%": round(100 * net / cost, 1) if cost else 0.0,
        })
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=int, default=None, help="restrict to one run number")
    args = ap.parse_args()

    rows = _load_rows()
    if args.run is not None:
        rows = [r for r in rows if r.get("_run") == args.run]

    runs_present = sorted({r.get("_run") for r in rows})
    lines: list[str] = []

    def out(s: str = "") -> None:
        print(s)
        lines.append(s)

    out("# Cohort comparison report\n")
    out(f"Runs: {runs_present}  |  raw rows: {len(rows):,}\n")

    summ = cohort_summary(rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summ.to_csv(OUT_DIR / "cohort_summary.csv", index=False)

    # Per-run cohort tables (run 5 = the live HOLD/TP experiment, same games each).
    for run in runs_present:
        sub = summ[summ["run"] == run]
        if sub.empty:
            continue
        tot_net = sub["net_$"].sum()
        out(f"## Run {run} — per-cohort (net ${tot_net:,.2f})\n")
        out(sub.drop(columns=["run"]).to_string(index=False))
        out("")

    # Entry-feature breakdowns (pooled), FIFO-paired.
    pos = fifo_positions(rows)
    if not pos.empty:
        out("## Entry-feature breakdowns (FIFO-paired positions, pooled)\n")
        out(f"Paired closed positions: {len(pos):,}  |  net ${pos['net_profit'].sum():,.2f}\n")

        out("### by ENTRY PRICE")
        out(_bucket(pos, "entry_price", [0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.41]).to_string(index=False))
        out("")

        if pos["hours_to_resolution"].notna().any():
            out("### by HOURS TO RESOLUTION (run 5+)")
            out(_bucket(pos, "hours_to_resolution", [0, 1, 3, 6, 12, 24]).to_string(index=False))
            out("")

        if pos["entry_spread"].notna().any():
            out("### by ENTRY SPREAD (run 5+)")
            out(_bucket(pos, "entry_spread", [0, 0.01, 0.02, 0.03, 0.05, 0.10, 1.0]).to_string(index=False))
            out("")

        out("### by SPORT")
        sp = pos.groupby("sport").agg(
            n=("net_profit", "size"), win_pct=("profitable", "mean"),
            net=("net_profit", "sum"), cost=("cost", "sum"),
        ).reset_index()
        sp["win%"] = (100 * sp["win_pct"]).round(1)
        sp["roi%"] = (100 * sp["net"] / sp["cost"]).round(1)
        out(sp[["sport", "n", "win%", "net"]].to_string(index=False))
        out("")

    ART_DIR.mkdir(parents=True, exist_ok=True)
    (ART_DIR / "cohort_report.md").write_text("\n".join(lines))
    print(f"\nSaved -> {ART_DIR / 'cohort_report.md'} and {OUT_DIR / 'cohort_summary.csv'}")


if __name__ == "__main__":
    main()
