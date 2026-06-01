from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class OpportunityType(str, Enum):
    ARBITRAGE = "arbitrage"
    MOMENTUM = "momentum"
    ENTRY = "entry"
    EXIT = "exit"


class Sport(str, Enum):
    NBA = "nba"
    MLB = "mlb"
    NHL = "nhl"
    UFC = "ufc"
    TENNIS_ATP = "tennis_atp"
    TENNIS_WTA = "tennis_wta"


@dataclass
class Market:
    ticker: str
    title: str
    yes_ask: float  # 0.0–1.0
    no_ask: float   # 0.0–1.0
    yes_bid: float = 0.0  # executable sell price for YES
    no_bid: float = 0.0   # executable sell price for NO
    sport: Optional[str] = None
    volume: float = 0.0
    resolves_at: Optional[float] = None  # unix ts; expected resolution (from Kalshi API)
    series_ticker: Optional[str] = None


@dataclass
class ScoreEvent:
    sport: Sport
    team: str
    old_score: int
    new_score: int
    opponent: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def delta(self) -> int:
        return self.new_score - self.old_score


@dataclass
class RunEvent:
    sport: Sport
    team: str
    points_scored: int
    opponent_scored: int
    window_seconds: float
    opponent: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class Position:
    ticker: str
    title: str
    side: str          # "yes" or "no"
    entry_price: float # 0.0–1.0
    count: int
    sport: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Opportunity:
    type: OpportunityType
    market: Market
    edge: float
    signal: str = ""
    suggested_side: str = "yes"
    suggested_count: int = 0
    position: Optional[Position] = None  # set for exit signals
    sell_profit: float = 0.0
    lock_profit: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def dedup_key(self) -> tuple:
        return (self.market.ticker, self.type)


@dataclass
class OrderResult:
    success: bool
    ticker: str
    side: str
    price: float
    count: int
    error: str = ""
