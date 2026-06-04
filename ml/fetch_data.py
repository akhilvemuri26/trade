"""Pull runs 2 & 3 trade ledgers from the live dashboard API into data/ml/raw/.

Run 2 is archived (served by /api/runs/{run_id}/trades).
Run 3 is the currently-running live run (no archive yet) -> served by /api/trades.

We page by `action` filter to beat the per-call row cap, then dedupe.

Usage:
    python -m ml.fetch_data
    DASHBOARD_URL=http://localhost:8080 python -m ml.fetch_data
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# python.org macOS builds often lack a CA bundle; this API is public + read-only.
_SSL_CTX = ssl._create_unverified_context()

DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL",
    "https://kalshi-trader-crimson-darkness-7593.fly.dev",
).rstrip("/")

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "ml" / "raw"
ACTIONS = ("buy", "sell", "lock", "settle")
PER_CALL_LIMIT = 5000


def _get_json(path: str, params: dict | None = None) -> dict:
    url = f"{DASHBOARD_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def list_runs() -> list[dict]:
    return _get_json("/api/runs").get("runs", [])


def _fetch_trades_paged(base_path: str) -> list[dict]:
    """Fetch all trades for an endpoint by querying each action separately.

    Endpoints cap the returned slice, but the action filter is applied before
    the slice, so per-action calls stay under the cap for these run sizes.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for action in ACTIONS:
        data = _get_json(base_path, {"action": action, "limit": PER_CALL_LIMIT, "order": "asc"})
        rows = data.get("trades", [])
        total = data.get("count")
        if total is not None and len(rows) < total:
            print(f"  WARNING: {action} returned {len(rows)} of {total} (capped) on {base_path}")
        for r in rows:
            # Dedupe key: a single ledger row is unique by (ticker, strategy, action, timestamp).
            key = (r.get("ticker"), r.get("strategy"), r.get("action"), r.get("timestamp"))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out


def fetch_run(run: dict) -> list[dict]:
    n = run.get("run_number")
    if run.get("status") == "running" or not run.get("archive_path"):
        print(f"Run {n} ({run['run_id']}): live run -> /api/trades")
        return _fetch_trades_paged("/api/trades")
    print(f"Run {n} ({run['run_id']}): archived -> /api/runs/{run['run_id']}/trades")
    return _fetch_trades_paged(f"/api/runs/{run['run_id']}/trades")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def main(run_numbers: list[int] | None = None) -> None:
    run_numbers = run_numbers or [2, 3]
    runs = {r.get("run_number"): r for r in list_runs()}
    for n in run_numbers:
        run = runs.get(n)
        if not run:
            print(f"Run {n}: not found in /api/runs, skipping")
            continue
        rows = fetch_run(run)
        out_path = RAW_DIR / f"run{n}_trades.jsonl"
        _write_jsonl(out_path, rows)
        from collections import Counter
        actions = Counter(r.get("action") for r in rows)
        print(f"  wrote {len(rows)} rows -> {out_path}  actions={dict(actions)}")


if __name__ == "__main__":
    nums = [int(a) for a in sys.argv[1:]] or None
    main(nums)
