"""Offline ledger validation for a simulation run.

Checks profit arithmetic, buy/exit pairing, filter compliance, and re-entry guard.

Usage:
  python scripts/validate_run.py data/ml/raw/run7_trades.jsonl
  python scripts/validate_run.py data/ml/raw/run7_trades.jsonl --config prod
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path

EXIT_ACTIONS = ("sell", "lock", "settle")

CONFIGS = {
    "prod": {
        "min_entry_price": 0.20,
        "underdog_max_price": 0.40,
        "min_hours_to_resolution": 3.0,
        "one_entry_per_game": True,
    },
    "run6": {
        "min_entry_price": 0.20,
        "underdog_max_price": 0.40,
        "min_hours_to_resolution": 3.0,
        "one_entry_per_game": True,
    },
}


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_trades(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        rows = []
        for line in path.open():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    data = json.loads(path.read_text())
    return data.get("trades", data if isinstance(data, list) else [])


def validate(trades: list[dict], cfg: dict) -> dict:
    issues: list[str] = []
    warnings: list[str] = []

    profit_mismatches = 0
    for t in trades:
        action = t.get("action")
        if action == "sell":
            ep = _f(t.get("entry_price"))
            xp = _f(t.get("exit_price") or t.get("price"))
            cnt = _f(t.get("count"))
            expected = round((xp - ep) * cnt, 2)
            recorded = round(_f(t.get("profit")), 2)
            if abs(expected - recorded) > 0.02:
                profit_mismatches += 1
                if profit_mismatches <= 5:
                    issues.append(
                        f"sell profit mismatch {t.get('ticker')}: "
                        f"expected={expected} recorded={recorded}"
                    )
        elif action == "settle":
            ep = _f(t.get("entry_price"))
            cnt = _f(t.get("count"))
            side = t.get("side", "yes")
            result = t.get("result")
            payout = t.get("payout")
            if payout is None:
                payout = cnt if result == side else 0.0
            payout = _f(payout)
            expected = round(payout - ep * cnt, 2)
            recorded = round(_f(t.get("profit")), 2)
            if abs(expected - recorded) > 0.02:
                profit_mismatches += 1
                if profit_mismatches <= 5:
                    issues.append(
                        f"settle profit mismatch {t.get('ticker')}: "
                        f"expected={expected} recorded={recorded}"
                    )
        elif action == "buy":
            price = _f(t.get("price"))
            cnt = _f(t.get("count"))
            cost = _f(t.get("cost"))
            if cost and abs(cost - round(price * cnt, 2)) > 0.02:
                issues.append(
                    f"buy cost mismatch {t.get('ticker')}: "
                    f"cost={cost} price*count={round(price * cnt, 2)}"
                )

    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for t in sorted(trades, key=lambda x: _f(x.get("timestamp"))):
        by_key[(t.get("strategy"), t.get("ticker"))].append(t)

    unclosed = []
    duplicate_buys = []
    orphan_exits = 0
    closed = 0
    for key, rows in by_key.items():
        buy_count = sum(1 for r in rows if r.get("action") == "buy")
        if buy_count > 1:
            duplicate_buys.append(key)
        ob: deque[dict] = deque()
        for r in rows:
            if r.get("action") == "buy":
                ob.append(r)
            elif r.get("action") in EXIT_ACTIONS:
                if not ob:
                    orphan_exits += 1
                else:
                    ob.popleft()
                    closed += 1
        if ob:
            unclosed.append((key, len(ob)))

    if orphan_exits:
        issues.append(f"{orphan_exits} exit(s) without matching buy")
    if cfg.get("one_entry_per_game") and duplicate_buys:
        for key in duplicate_buys[:5]:
            issues.append(f"re-entry guard violation: {key}")
        if len(duplicate_buys) > 5:
            issues.append(f"... and {len(duplicate_buys) - 5} more re-entry violations")

    buys = [t for t in trades if t.get("action") == "buy"]
    below_price = [b for b in buys if _f(b.get("price"), 1.0) < cfg.get("min_entry_price", 0)]
    above_cap = [b for b in buys if _f(b.get("price")) > cfg.get("underdog_max_price", 1.0)]
    near_res = [
        b for b in buys
        if b.get("hours_to_resolution") is not None
        and _f(b["hours_to_resolution"]) < cfg.get("min_hours_to_resolution", 0)
    ]
    for b in below_price[:3]:
        warnings.append(f"below min_entry_price: {b.get('ticker')} @ {b.get('price')}")
    for b in above_cap[:3]:
        warnings.append(f"above underdog_max: {b.get('ticker')} @ {b.get('price')}")
    for b in near_res[:3]:
        warnings.append(
            f"below min_hours_to_resolution: {b.get('ticker')} "
            f"hours={b.get('hours_to_resolution')}"
        )

    realized = sum(_f(t.get("profit")) for t in trades if t.get("action") in EXIT_ACTIONS)

    return {
        "total_rows": len(trades),
        "buys": len(buys),
        "closed_positions": closed,
        "open_positions": len(unclosed),
        "realized_pl": round(realized, 2),
        "profit_mismatches": profit_mismatches,
        "duplicate_buy_pairs": len(duplicate_buys),
        "filter_violations": {
            "below_min_price": len(below_price),
            "above_underdog_max": len(above_cap),
            "below_min_hours": len(near_res),
        },
        "issues": issues,
        "warnings": warnings,
        "ok": len(issues) == 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="run{N}_trades.jsonl or trades JSON")
    ap.add_argument(
        "--config", choices=list(CONFIGS), default="prod",
        help="filter thresholds to check against (default: prod)",
    )
    args = ap.parse_args()
    if not args.path.exists():
        print(f"File not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    trades = load_trades(args.path)
    result = validate(trades, CONFIGS[args.config])

    print(f"Validation: {args.path.name}  config={args.config}")
    print(f"  rows={result['total_rows']}  buys={result['buys']}  "
          f"closed={result['closed_positions']}  open={result['open_positions']}")
    print(f"  realized P&L: ${result['realized_pl']:.2f}")
    print(f"  profit mismatches: {result['profit_mismatches']}")
    print(f"  re-entry violations: {result['duplicate_buy_pairs']}")
    fv = result["filter_violations"]
    print(f"  filter violations: price<{CONFIGS[args.config]['min_entry_price']}={fv['below_min_price']}  "
          f"hours<{CONFIGS[args.config]['min_hours_to_resolution']}={fv['below_min_hours']}")

    if result["warnings"]:
        print("\nWarnings:")
        for w in result["warnings"]:
            print(f"  - {w}")
        if fv["below_min_hours"] > 3:
            print(f"  ... and {fv['below_min_hours'] - 3} more hours violations")

    if result["issues"]:
        print("\nISSUES:")
        for i in result["issues"]:
            print(f"  - {i}")
        sys.exit(1)

    print("\nOK — no structural ledger issues")
    sys.exit(0)


if __name__ == "__main__":
    main()
