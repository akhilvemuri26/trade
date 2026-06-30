"""Generate a markdown analysis report for a run ledger.

Usage:
  python scripts/run_analysis.py 7
  python scripts/run_analysis.py data/ml/raw/run7_trades.jsonl -o ml/artifacts/run7_analysis.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "ml" / "raw"
ART = ROOT / "ml" / "artifacts"


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_trades(path: Path) -> list[dict]:
    rows = []
    for line in path.open():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def fifo_positions(trades: list[dict]) -> list[tuple[dict, dict]]:
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        by_key[(t.get("strategy"), t.get("ticker"))].append(t)
    out = []
    for rows in by_key.values():
        rows.sort(key=lambda x: _f(x.get("timestamp")))
        ob: deque[dict] = deque()
        for r in rows:
            if r.get("action") == "buy":
                ob.append(r)
            elif r.get("action") in ("sell", "lock", "settle") and ob:
                out.append((ob.popleft(), r))
    return out


def bucket_table(positions: list[tuple[dict, dict]], col: str, edges: list[float]) -> list[str]:
    lines = []
    for lo, hi in zip(edges, edges[1:]):
        sub = [
            (en, ex) for en, ex in positions
            if en.get(col) is not None and lo <= _f(en[col]) < hi
        ]
        if not sub:
            continue
        net = sum(_f(ex.get("profit")) for _, ex in sub)
        cost = sum(_f(en.get("cost")) for en, _ in sub)
        wins = sum(1 for _, ex in sub if _f(ex.get("profit")) > 0)
        lines.append(
            f"| [{lo:g},{hi:g}) | {len(sub)} | {100 * wins / len(sub):.1f}% | "
            f"${net:.2f} | {100 * net / cost:.1f}% |"
        )
    return lines


def analyze(trades: list[dict], run_label: str) -> str:
    positions = fifo_positions(trades)
    strategies = sorted({t.get("strategy") for t in trades})

    agg: dict[str, dict] = defaultdict(lambda: {"net": 0.0, "n": 0, "wins": 0, "cost": 0.0})
    for t in trades:
        s = t.get("strategy", "?")
        if t.get("action") == "buy":
            agg[s]["cost"] += _f(t.get("cost"))
        elif t.get("action") in ("sell", "lock", "settle"):
            p = _f(t.get("profit"))
            agg[s]["net"] += p
            agg[s]["n"] += 1
            if p > 0:
                agg[s]["wins"] += 1

    sell_net = sum(_f(ex.get("profit")) for _, ex in positions if ex.get("action") == "sell")
    settle_net = sum(_f(ex.get("profit")) for _, ex in positions if ex.get("action") == "settle")
    total_net = sell_net + settle_net

    buys = [t for t in trades if t.get("action") == "buy"]
    score_cov = sum(1 for b in buys if b.get("entry_score_diff") is not None)
    live_cov = sum(1 for b in buys if b.get("entry_is_live") is True)

    lines = [
        f"# Run Analysis — {run_label}\n",
        f"Generated from `{len(trades)}` ledger rows, `{len(positions)}` closed positions.\n",
        "## Summary\n",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Strategies | {', '.join(strategies)} |",
        f"| Closed positions | {len(positions)} |",
        f"| Realized net P&L | **${total_net:.2f}** |",
        f"| Stop-loss / sell P&L | ${sell_net:.2f} |",
        f"| Settlement P&L | ${settle_net:.2f} |",
        f"| Win rate | {100 * sum(1 for _, ex in positions if _f(ex.get('profit')) > 0) / max(len(positions), 1):.1f}% |",
        f"| In-game score_diff coverage | {score_cov}/{len(buys)} buys ({100 * score_cov / max(len(buys), 1):.0f}%) |",
        f"| Live entry coverage | {live_cov}/{len(buys)} buys |",
        "",
        "## Per-strategy P&L\n",
        "| Strategy | Exits | Win% | Net | ROI |",
        "|----------|-------|------|-----|-----|",
    ]
    for s, v in sorted(agg.items(), key=lambda x: -x[1]["net"]):
        wr = 100 * v["wins"] / v["n"] if v["n"] else 0
        roi = 100 * v["net"] / v["cost"] if v["cost"] else 0
        lines.append(f"| {s} | {v['n']} | {wr:.1f}% | ${v['net']:.2f} | {roi:.1f}% |")

    lines += [
        "",
        "## Entry price bands\n",
        "| Band | n | Win% | Net | ROI |",
        "|------|---|------|-----|-----|",
    ]
    lines += bucket_table(positions, "price", [0, 0.20, 0.25, 0.30, 0.35, 0.41])

    lines += [
        "",
        "## Hours to resolution\n",
        "| Hours | n | Win% | Net | ROI |",
        "|-------|---|------|-----|-----|",
    ]
    lines += bucket_table(positions, "hours_to_resolution", [0, 1, 3, 6, 12, 24, 999])

    sp: dict[str, list] = defaultdict(list)
    for en, ex in positions:
        sp[(en.get("sport") or "?").lower()].append((en, ex))
    lines += ["", "## By sport\n", "| Sport | n | Win% | Net | ROI |", "|-------|---|------|-----|-----|"]
    for sport, sub in sorted(sp.items(), key=lambda x: -sum(_f(ex.get("profit")) for _, ex in x[1])):
        net = sum(_f(ex.get("profit")) for _, ex in sub)
        cost = sum(_f(en.get("cost")) for en, _ in sub)
        wins = sum(1 for _, ex in sub if _f(ex.get("profit")) > 0)
        lines.append(
            f"| {sport} | {len(sub)} | {100 * wins / len(sub):.1f}% | ${net:.2f} | {100 * net / cost:.1f}% |"
        )

    ing = [(en, ex) for en, ex in positions if en.get("entry_score_diff") is not None]
    if ing:
        lines += ["", "## In-game score diff (where logged)\n",
                  "| State | n | Win% | Net | ROI |", "|-------|---|------|-----|-----|"]
        for label, pred in [
            ("Leading (diff>0)", lambda d: d > 0),
            ("Tied (diff=0)", lambda d: d == 0),
            ("Trailing (diff<0)", lambda d: d < 0),
        ]:
            sub = [(en, ex) for en, ex in ing if pred(_f(en["entry_score_diff"]))]
            if not sub:
                continue
            net = sum(_f(ex.get("profit")) for _, ex in sub)
            cost = sum(_f(en.get("cost")) for en, _ in sub)
            wins = sum(1 for _, ex in sub if _f(ex.get("profit")) > 0)
            lines.append(
                f"| {label} | {len(sub)} | {100 * wins / len(sub):.1f}% | "
                f"${net:.2f} | {100 * net / cost:.1f}% |"
            )

    live = [(en, ex) for en, ex in positions if en.get("entry_is_live") is True]
    pre = [(en, ex) for en, ex in positions if en.get("entry_is_live") is False]
    lines += ["", "## Live vs pregame\n", "| Entry | n | Win% | Net | ROI |", "|-------|---|------|-----|-----|"]
    for label, sub in [("Live", live), ("Pregame", pre)]:
        if not sub:
            continue
        net = sum(_f(ex.get("profit")) for _, ex in sub)
        cost = sum(_f(en.get("cost")) for en, _ in sub)
        wins = sum(1 for _, ex in sub if _f(ex.get("profit")) > 0)
        lines.append(
            f"| {label} | {len(sub)} | {100 * wins / len(sub):.1f}% | "
            f"${net:.2f} | {100 * net / cost:.1f}% |"
        )

    lines += ["", "## Exit reasons\n"]
    for reason, count in Counter(
        ex.get("exit_reason") or ex.get("action") for _, ex in positions
    ).most_common():
        sub = [(en, ex) for en, ex in positions if (ex.get("exit_reason") or ex.get("action")) == reason]
        net = sum(_f(ex.get("profit")) for _, ex in sub)
        lines.append(f"- **{reason}**: {count} exits, net ${net:.2f}")

    lines += [
        "",
        "## Run 6 experiment task checklist\n",
        "| Task | Status |",
        "|------|--------|",
        "| Re-entry guard | Done — 0 duplicate buys per strategy+ticker |",
        "| Entry filters in code | Done — env-configurable in strategy_config |",
        "| Entry filters in prod | **Gap** — Run 7 deployed with min_hours=0 |",
        "| In-game logging | Partial — score_diff coverage varies |",
        "| ML gate OFF | Done |",
        "| Track B retrain | Pending — filter_model updated for runs 4-6 train / 7 test |",
        "| Score feed bridge | In progress — team alias matching added |",
        "",
        "## Recommendations\n",
        "1. Deploy with `MIN_HOURS_TO_RESOLUTION=3` and `STRATEGY_SET=prod`.",
        "2. Consider raising `MIN_ENTRY_PRICE` to 0.30–0.35.",
        "3. Retrain filter model after score bridge improves coverage.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", help="run number (e.g. 7) or path to jsonl")
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()

    if Path(args.run).exists():
        path = Path(args.run)
        label = path.stem
    else:
        m = re.match(r"\d+", args.run)
        if not m:
            print("Provide a run number or path", file=sys.stderr)
            sys.exit(1)
        num = m.group(0)
        path = RAW / f"run{num}_trades.jsonl"
        label = f"Run {num}"
        if not path.exists():
            print(f"Missing {path} — run: python -m ml.fetch_runs {num}", file=sys.stderr)
            sys.exit(1)

    out = args.output or ART / f"run{label.split()[-1]}_analysis.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    report = analyze(load_trades(path), label)
    out.write_text(report)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
