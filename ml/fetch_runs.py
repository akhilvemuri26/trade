"""Pull a run's trade ledger from the live dashboard into data/ml/raw/run{N}_trades.jsonl.

Runs 2-3 already exist locally as raw ledgers. Runs 4+ live on the Fly volume; we
read them through the **public** dashboard API (no SSH):

  - archived runs (e.g. run 4)  -> GET /api/runs/{run_id}/trades   (served from the archive)
  - the live / current run       -> GET /api/trades                 (current in-memory ledger)

Both endpoints return rows via run_server._normalize_trades, which COPIES the full raw
ledger row and only *adds* display fields, so price/count/cost/profit/sport/title/timestamp
and the entry_* microstructure fields are all preserved.

Usage:
  python -m ml.fetch_runs            # fetch every run not already present locally (4, 5, ...)
  python -m ml.fetch_runs 4 5        # fetch specific run numbers (overwrites those files)
  python -m ml.fetch_runs --all      # refetch all runs from the dashboard
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx  # bundles certifi -> works where stdlib urllib lacks the macOS cert store

BASE = "https://kalshi-trader-crimson-darkness-7593.fly.dev"
RAW = Path(__file__).resolve().parent.parent / "data" / "ml" / "raw"


def _get(path: str) -> dict:
    r = httpx.get(BASE + path, timeout=45, headers={"User-Agent": "ml.fetch_runs"})
    r.raise_for_status()
    return r.json()


def list_runs() -> list[dict]:
    d = _get("/api/runs")
    return d.get("runs", d) if isinstance(d, dict) else d


def _trades_for(run: dict) -> list[dict]:
    rid = run["run_id"]
    if run.get("archive_path"):
        data = _get(f"/api/runs/{rid}/trades?limit=5000")
    else:
        # Live/current run has no archive yet; its ledger is /api/trades.
        data = _get("/api/trades?limit=2000")
    return data.get("trades", []) if isinstance(data, dict) else (data or [])


def fetch_run(run: dict) -> tuple[int, int, Path]:
    num = int(run["run_number"])
    trades = _trades_for(run)
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / f"run{num}_trades.jsonl"
    with out.open("w") as f:
        for t in trades:
            f.write(json.dumps(t, default=str) + "\n")
    return num, len(trades), out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("runs", nargs="*", type=int, help="run numbers to fetch")
    ap.add_argument("--all", action="store_true", help="refetch every run")
    args = ap.parse_args()

    runs = list_runs()
    by_num = {int(r["run_number"]): r for r in runs}

    if args.runs:
        targets = [by_num[n] for n in args.runs if n in by_num]
        missing = [n for n in args.runs if n not in by_num]
        if missing:
            print(f"  (no such run(s): {missing}; available: {sorted(by_num)})")
    elif args.all:
        targets = runs
    else:
        # Default: only runs we don't already have a local raw ledger for.
        targets = [r for r in runs if not (RAW / f"run{int(r['run_number'])}_trades.jsonl").exists()]

    if not targets:
        print(f"Nothing to fetch. Local raw runs: "
              f"{sorted(int(p.name[3]) for p in RAW.glob('run*_trades.jsonl'))}")
        return

    for r in sorted(targets, key=lambda x: int(x["run_number"])):
        num, n, out = fetch_run(r)
        print(f"run {num} ({r['run_id']}, {r.get('status')}): {n} trades -> {out.relative_to(RAW.parent.parent.parent)}")


if __name__ == "__main__":
    main()
