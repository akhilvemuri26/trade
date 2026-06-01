"""
End-to-end smoke test: observe markets → paper execute → verify logs.

Run while run_server.py is NOT required (self-contained).
"""

import asyncio
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from kalshi_client import KalshiClient
from paper_executor import PaperExecutor
from virtual_trader import VirtualTrader


async def main() -> int:
    paper = PaperExecutor(5000.0)
    run_id = str(int(time.time()))
    config = {
        "name": f"e2e_{run_id}",
        "underdog_max_price": 0.50,
        "max_risk": 25,
        "max_positions": 3,
        "min_volume": 100,
        "run_pts": 99,
        "run_opp": 99,
        "run_window": 60,
        "sports": ["nba", "mlb", "nhl"],
    }

    async with KalshiClient() as kalshi:
        markets = await kalshi.get_sports_markets()
        if not markets:
            print("FAIL: no sports markets returned")
            return 1

        with_vol = [m for m in markets if m.volume >= config["min_volume"]]
        tradeable = [
            m for m in with_vol
            if 0 < m.yes_ask <= config["underdog_max_price"]
        ]
        print(f"Markets: {len(markets)} total, {len(with_vol)} vol>={config['min_volume']}, "
              f"{len(tradeable)} tradeable underdogs")
        if markets[0].volume > 0:
            top = markets[0]
            print(f"Top by volume: vol={top.volume:.0f} yes={top.yes_ask:.3f} {top.title[:60]}")

        if not tradeable:
            print("WARN: no tradeable underdog markets right now (prices/volume); skipping entry test")
            return 0

        trader = VirtualTrader(config, kalshi, paper=paper)
        before = paper.get_balance()
        await trader.check_entries(markets, all_traders=[trader])
        after = paper.get_balance()
        s = trader.summary()
        log = paper.get_trade_log()

        print(f"Balance: ${before:.2f} → ${after:.2f}")
        print(f"Strategy entries: {s['entries']}, paper log rows: {len(log)}")

        if s["entries"] < 1 or len(log) < 1:
            print("FAIL: expected at least one paper buy")
            return 1

        if log[-1].get("strategy") != config["name"]:
            print("FAIL: trade log missing strategy tag")
            return 1

        path = trader.save_results()
        print(f"OK: end-to-end paper trade saved to {path}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
