import json
import logging
import os
from pathlib import Path
from typing import Optional

from models import Position

logger = logging.getLogger(__name__)

POSITIONS_FILE = Path(__file__).parent / "positions.json"


class PositionStore:
    def __init__(self, path: Path = POSITIONS_FILE):
        self._path = path
        self._positions: dict[str, Position] = {}
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for ticker, p in data.items():
                self._positions[ticker] = Position(**p)
        except Exception as e:
            logger.warning("Failed to load positions: %s", e)

    def _save(self):
        data = {}
        for ticker, p in self._positions.items():
            data[ticker] = {
                "ticker": p.ticker,
                "title": p.title,
                "side": p.side,
                "entry_price": p.entry_price,
                "count": p.count,
                "sport": p.sport,
                "timestamp": p.timestamp,
            }
        self._path.write_text(json.dumps(data, indent=2))

    def add(self, position: Position):
        self._positions[position.ticker] = position
        self._save()
        logger.info("Position opened: %s %s×%d @ %.2f", position.ticker, position.side, position.count, position.entry_price)

    def remove(self, ticker: str):
        if ticker in self._positions:
            del self._positions[ticker]
            self._save()
            logger.info("Position closed: %s", ticker)

    def get(self, ticker: str) -> Optional[Position]:
        return self._positions.get(ticker)

    def get_all(self) -> list[Position]:
        return list(self._positions.values())

    def has_position(self, ticker: str) -> bool:
        return ticker in self._positions
