"""Persist simulation manifest, paper wallet state, and append-only trade ledger."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from paths import DATA_DIR, ensure_data_dirs

MANIFEST_PATH = DATA_DIR / "simulation_manifest.json"
PAPER_STATE_PATH = DATA_DIR / "paper_state.json"
LEDGER_DIR = DATA_DIR / "ledger"
LEDGER_PATH = LEDGER_DIR / "trades.jsonl"
ARCHIVE_DIR = DATA_DIR / "archive"
RUNS_INDEX_PATH = DATA_DIR / "runs.json"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_simulation_dirs() -> None:
    ensure_data_dirs()
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def load_runs_index() -> list[dict]:
    if not RUNS_INDEX_PATH.exists():
        return []
    try:
        return json.loads(RUNS_INDEX_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_runs_index(runs: list[dict]) -> None:
    ensure_simulation_dirs()
    RUNS_INDEX_PATH.write_text(json.dumps(runs, indent=2, default=str))


def next_run_number() -> int:
    runs = load_runs_index()
    if not runs:
        return 1
    return max(r.get("run_number", 0) for r in runs) + 1


def register_run(manifest: dict, *, status: str = "running") -> dict:
    runs = load_runs_index()
    run_number = manifest.get("run_number", next_run_number())
    entry = {
        "run_number": run_number,
        "run_id": manifest["run_id"],
        "label": manifest.get("run_label", f"Run {run_number}"),
        "started_at": manifest["started_at"],
        "ended_at": None,
        "status": status,
        "initial_balance": manifest.get("paper_initial_balance"),
        "archive_path": None,
        "final_portfolio": None,
    }
    existing = [r for r in runs if r["run_id"] != manifest["run_id"]]
    existing.append(entry)
    existing.sort(key=lambda r: r.get("run_number", 0))
    save_runs_index(existing)
    return entry


def archive_run_in_index(run_id: str, archive_path: str, final_portfolio: dict | None = None) -> None:
    runs = load_runs_index()
    for r in runs:
        if r["run_id"] == run_id:
            r["status"] = "archived"
            r["ended_at"] = _utc_iso()
            r["archive_path"] = archive_path
            if final_portfolio:
                r["final_portfolio"] = final_portfolio
            break
    save_runs_index(runs)


def create_manifest(strategies: list[dict], paper_balance: float) -> dict:
    run_number = next_run_number()
    manifest = {
        "run_id": str(uuid.uuid4())[:8],
        "run_number": run_number,
        "run_label": f"Run {run_number}",
        "experiment": "underdog_swing_exit",
        "started_at": _utc_iso(),
        "ended_at": None,
        "status": "running",
        "paper_initial_balance": paper_balance,
        "strategies": strategies,
    }
    save_manifest(manifest)
    register_run(manifest)
    return manifest


def load_manifest() -> Optional[dict]:
    if not MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_manifest(manifest: dict) -> None:
    ensure_simulation_dirs()
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, default=str))


def mark_manifest_stopped(final_pl: Optional[dict] = None) -> None:
    manifest = load_manifest() or {}
    manifest["status"] = "stopped"
    manifest["ended_at"] = _utc_iso()
    if final_pl is not None:
        manifest["final_portfolio"] = final_pl
    save_manifest(manifest)


def save_paper_state(paper) -> None:
    ensure_simulation_dirs()
    payload = {
        "updated_at": _utc_iso(),
        "balance": paper.get_balance(),
        "initial_balance": paper.get_initial_balance(),
        "trade_log": paper.get_trade_log(),
    }
    PAPER_STATE_PATH.write_text(json.dumps(payload, indent=2, default=str))


def load_paper_state() -> Optional[dict]:
    if not PAPER_STATE_PATH.exists():
        return None
    try:
        return json.loads(PAPER_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def restore_paper_executor(paper) -> bool:
    """Restore balance and trade log from disk if present."""
    state = load_paper_state()
    if not state:
        return False
    paper._balance = float(state.get("balance", paper._balance))
    paper._initial_balance = float(
        state.get("initial_balance", paper._initial_balance),
    )
    paper._trade_log = list(state.get("trade_log", []))
    return True


def load_ledger_trades(limit: int = 500) -> list[dict]:
    """Read recent rows from trades.jsonl (newest last)."""
    if not LEDGER_PATH.exists():
        return []
    lines = LEDGER_PATH.read_text().strip().splitlines()
    if not lines:
        return []
    rows = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_all_ledger_trades() -> list[dict]:
    """Read all ledger rows in order (oldest -> newest)."""
    return load_ledger_trades(limit=10_000_000)


def append_ledger(entry: dict) -> None:
    ensure_simulation_dirs()
    line = {**entry, "recorded_at": time.time(), "iso": _utc_iso()}
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(line, default=str) + "\n")


def rebuild_strategy_trades_from_ledger(traders: list) -> dict[str, int]:
    """
    Rehydrate in-memory trader histories from persisted ledger.
    Returns count of rows replayed per strategy.
    """
    rows = load_all_ledger_trades()
    by_strategy = {t.name: t for t in traders}
    replayed = {t.name: 0 for t in traders}
    for trader in traders:
        # Keep current in-memory rows if any new ones already exist.
        if not getattr(trader, "_trades", None):
            trader._trades = []
    for row in rows:
        strategy = row.get("strategy")
        if strategy not in by_strategy:
            continue
        action = row.get("action")
        if action not in ("buy", "sell", "lock", "settle"):
            continue
        trade = {k: v for k, v in row.items() if k not in ("recorded_at", "iso")}
        by_strategy[strategy]._trades.append(trade)
        replayed[strategy] += 1
    return replayed


def record_trade(
    *,
    strategy: str,
    action: str,
    ticker: str,
    exit_mode: str = "",
    exit_reason: str = "",
    profit: Optional[float] = None,
    extra: Optional[dict] = None,
) -> None:
    row: dict[str, Any] = {
        "strategy": strategy,
        "action": action,
        "ticker": ticker,
        "exit_mode": exit_mode,
        "exit_reason": exit_reason,
    }
    if profit is not None:
        row["profit"] = profit
    if extra:
        row.update(extra)
    append_ledger(row)


def load_archived_run(archive_path: str) -> dict | None:
    """Read manifest, paper state, and trades from an archive directory."""
    base = DATA_DIR / archive_path
    if not base.exists():
        return None
    result: dict[str, Any] = {}
    manifest_path = base / "simulation_manifest.json"
    if manifest_path.exists():
        try:
            result["manifest"] = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    paper_path = base / "paper_state.json"
    if paper_path.exists():
        try:
            result["paper_state"] = json.loads(paper_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return result if result else None


def load_archived_trades(archive_path: str, limit: int = 5000) -> list[dict]:
    """Read trades from an archived trades.jsonl file."""
    ledger = DATA_DIR / archive_path / "trades.jsonl"
    if not ledger.exists():
        return []
    lines = ledger.read_text().strip().splitlines()
    rows = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _copy_if_exists(src: Path, dest: Path) -> None:
    import shutil

    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def purge_orphan_positions(active_strategy_names: set[str]) -> list[str]:
    """Remove position files for strategies no longer in the experiment."""
    from paths import POSITIONS_DIR

    removed: list[str] = []
    if not POSITIONS_DIR.exists():
        return removed
    for path in POSITIONS_DIR.glob("*.json"):
        if path.stem not in active_strategy_names:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def archive_and_clear_positions() -> Optional[Path]:
    """Archive positions/ and results/, then clear active position files."""
    from paths import POSITIONS_DIR, RESULTS_DIR

    ensure_simulation_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = ARCHIVE_DIR / f"pre_swing_experiment_{ts}"
    had_data = POSITIONS_DIR.exists() and any(POSITIONS_DIR.glob("*.json"))
    if not had_data and not RESULTS_DIR.exists():
        return None

    dest.mkdir(parents=True, exist_ok=True)
    _copy_if_exists(POSITIONS_DIR, dest / "positions")
    _copy_if_exists(RESULTS_DIR, dest / "results")
    if POSITIONS_DIR.exists():
        for p in POSITIONS_DIR.glob("*.json"):
            p.unlink()
    return dest


def archive_and_reset_simulation(
    paper,
    initial_balance: float,
    strategies: list[dict],
) -> Path:
    """
    Archive all simulation data, reset paper wallet to initial_balance, new manifest.
    Use on FRESH_EXPERIMENT=1 for a clean run.
    """
    from paths import POSITIONS_DIR, RESULTS_DIR

    ensure_simulation_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = ARCHIVE_DIR / f"sim_reset_{ts}"
    dest.mkdir(parents=True, exist_ok=True)

    old_manifest = load_manifest()

    _copy_if_exists(POSITIONS_DIR, dest / "positions")
    _copy_if_exists(RESULTS_DIR, dest / "results")
    _copy_if_exists(PAPER_STATE_PATH, dest / "paper_state.json")
    _copy_if_exists(MANIFEST_PATH, dest / "simulation_manifest.json")
    _copy_if_exists(LEDGER_PATH, dest / "trades.jsonl")

    if old_manifest:
        old_run_id = old_manifest.get("run_id", "")
        runs = load_runs_index()
        if not any(r["run_id"] == old_run_id for r in runs):
            old_manifest.setdefault("run_number", 1)
            old_manifest.setdefault("run_label", "Run 1")
            register_run(old_manifest, status="running")
        archive_run_in_index(
            old_run_id,
            str(dest.relative_to(dest.parent.parent)),
        )

    if POSITIONS_DIR.exists():
        for p in POSITIONS_DIR.glob("*.json"):
            p.unlink()
    PAPER_STATE_PATH.unlink(missing_ok=True)
    MANIFEST_PATH.unlink(missing_ok=True)
    if LEDGER_PATH.exists():
        LEDGER_PATH.unlink()

    paper._balance = initial_balance
    paper._initial_balance = initial_balance
    paper._trade_log = []

    active = {s["name"] for s in strategies}
    purge_orphan_positions(active)
    save_paper_state(paper)
    return dest
