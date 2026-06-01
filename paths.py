"""Data directories — use DATA_DIR=/data on Fly.io (mounted volume)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT)))
RESULTS_DIR = DATA_DIR / "results"
POSITIONS_DIR = DATA_DIR / "positions"
LOGS_DIR = RESULTS_DIR / "logs"


def ensure_data_dirs() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    POSITIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "ledger").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "archive").mkdir(parents=True, exist_ok=True)
