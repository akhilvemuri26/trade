import logging
import os
import time
from typing import Optional

from rapidfuzz import process as fuzz_process

from kalshi_client import KalshiClient
from models import Market, Opportunity, OpportunityType, Position, RunEvent, Sport
from positions import PositionStore

logger = logging.getLogger(__name__)

MAX_RISK = float(os.getenv("MAX_RISK_PER_TRADE", "50"))
UNDERDOG_MAX_PRICE = float(os.getenv("UNDERDOG_MAX_PRICE", "0.45"))
FUZZY_THRESHOLD = 70


class MomentumStrategy:
    def __init__(self, kalshi: KalshiClient, positions: PositionStore):
        self._kalshi = kalshi
        self._positions = positions
        self._markets_cache: list[Market] = []
        self._cache_ts: float = 0

    async def _get_markets(self) -> list[Market]:
        if time.time() - self._cache_ts > 60:
            self._markets_cache = await self._kalshi.get_sports_markets()
            self._cache_ts = time.time()
        return self._markets_cache

    def _find_market(self, markets: list[Market], team: str, sport: str) -> Optional[Market]:
        candidates = [m for m in markets if m.sport == sport]
        if not candidates:
            return None
        titles = [m.title for m in candidates]
        result = fuzz_process.extractOne(team, titles, score_cutoff=FUZZY_THRESHOLD)
        if not result:
            return None
        _, _, idx = result
        return candidates[idx]

    async def evaluate_entry(self, markets: list[Market]) -> list[Opportunity]:
        """
        Scan for underdog entry opportunities.
        Returns buy recommendations for markets where the underdog YES price is low enough.
        """
        entries: list[Opportunity] = []
        for market in markets:
            if self._positions.has_position(market.ticker):
                continue

            # Underdog = the side with lower YES price
            if market.yes_ask <= UNDERDOG_MAX_PRICE:
                count = int(MAX_RISK / market.yes_ask) if market.yes_ask > 0 else 0
                if count < 1:
                    continue
                entries.append(Opportunity(
                    type=OpportunityType.ENTRY,
                    market=market,
                    edge=1.0 - market.yes_ask,  # potential upside if YES resolves
                    signal=f"Underdog YES at ${market.yes_ask:.2f} — potential {(1.0 - market.yes_ask) * 100:.0f}% return",
                    suggested_side="yes",
                    suggested_count=count,
                ))

        return entries

    async def evaluate_exit(self, run: RunEvent) -> Optional[Opportunity]:
        """
        When a run is detected, check if we hold a position on the running team.
        If so, calculate exit options (sell at profit or lock via opposite side).
        """
        markets = await self._get_markets()
        market = self._find_market(markets, run.team, run.sport.value)
        if not market:
            return None

        # Re-fetch for latest price
        refreshed = await self._kalshi.get_market(market.ticker)
        if not refreshed:
            return None

        position = self._positions.get(refreshed.ticker)
        if not position:
            return None

        if position.side == "yes":
            current_price = refreshed.yes_ask
            opposite_price = refreshed.no_ask
        else:
            current_price = refreshed.no_ask
            opposite_price = refreshed.yes_ask

        sell_profit = (current_price - position.entry_price) * position.count
        lock_profit = (1.0 - position.entry_price - opposite_price) * position.count

        if sell_profit <= 0 and lock_profit <= 0:
            return None

        return Opportunity(
            type=OpportunityType.EXIT,
            market=refreshed,
            edge=max(sell_profit, lock_profit) / (position.entry_price * position.count),
            signal=(
                f"{run.team} on a {run.points_scored}-{run.opponent_scored} run "
                f"in {run.window_seconds:.0f}s!"
            ),
            position=position,
            sell_profit=sell_profit,
            lock_profit=lock_profit,
        )
