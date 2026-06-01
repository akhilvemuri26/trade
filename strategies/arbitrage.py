import os
from typing import Iterator

from models import Market, Opportunity, OpportunityType

# Minimum edge after estimated fees (~1% per side on Kalshi)
MIN_EDGE = float(os.getenv("ARBITRAGE_MIN_EDGE", "0.02"))


def detect(markets: list[Market]) -> Iterator[Opportunity]:
    """Yield arbitrage opportunities where YES_ask + NO_ask < 1 - MIN_EDGE."""
    for market in markets:
        total = market.yes_ask + market.no_ask
        edge = 1.0 - total
        if edge >= MIN_EDGE:
            yield Opportunity(
                type=OpportunityType.ARBITRAGE,
                market=market,
                edge=edge,
                signal=(
                    f"YES({market.yes_ask:.2f}) + NO({market.no_ask:.2f}) "
                    f"= {total:.2f} → {edge * 100:.1f}% risk-free edge"
                ),
            )
