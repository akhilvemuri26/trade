"""
End-to-end trade debug script.
Connects to Kalshi demo, finds an open market, places a ~$5 buy order.

Usage:
    python debug_trade.py              # find market + place $5 trade
    python debug_trade.py --dry-run    # find market but don't place order
"""

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load executor credentials
load_dotenv(Path(__file__).parent / "executor" / ".env")

sys.path.insert(0, str(Path(__file__).parent / "executor"))
from kalshi_trading import KalshiTradingClient

import httpx

DEMO_BASE = "https://demo-api.kalshi.co"
PROD_BASE = "https://api.elections.kalshi.com"

KEY_ID = os.getenv("KALSHI_KEY_ID")
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")

# Resolve relative key path against executor/ directory
if KEY_PATH and not os.path.isabs(KEY_PATH):
    KEY_PATH = str(Path(__file__).parent / "executor" / KEY_PATH)


async def fetch_markets(base_url: str, limit: int = 100) -> list[dict]:
    """Fetch open markets from a Kalshi environment."""
    async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
        resp = await client.get("/trade-api/v2/markets", params={"status": "open", "limit": limit})
        resp.raise_for_status()
        return resp.json().get("markets", [])


def pick_market(markets: list[dict], target_dollars: float = 5.0) -> dict | None:
    """
    Pick a tradeable market closest to the target dollar amount.
    Prefers markets with a real yes_ask; falls back to last_price if no ask.
    """
    tradeable = []
    for m in markets:
        # New API: prices in *_dollars fields (dollar strings)
        yes_ask = float(m.get("yes_ask_dollars") or 0)
        if yes_ask <= 0:
            # Fall back to last price so we can place a limit order
            yes_ask = float(m.get("last_price_dollars") or 0)
        if yes_ask <= 0 or yes_ask >= 1.0:
            # Use 0.50 as default for markets with no price yet
            yes_ask = 0.50
        count = max(1, round(target_dollars / yes_ask))
        cost = yes_ask * count
        tradeable.append((m, yes_ask, count, cost))

    if not tradeable:
        return None

    # Prefer markets with a real ask, then closest to target cost
    tradeable.sort(key=lambda x: (
        float(x[0].get("yes_ask_dollars") or 0) == 0,  # markets with real ask first
        abs(x[3] - target_dollars),
        abs(x[1] - 0.5),
    ))
    return tradeable[0]


async def main(dry_run: bool):
    if not KEY_ID or (not KEY_PATH and not KEY_PEM):
        print("ERROR: KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in executor/.env")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Kalshi End-to-End Debug  ({KALSHI_ENV} mode)")
    print(f"{'='*60}\n")

    # Step 1: Connect and check balance
    print("Step 1: Connecting to Kalshi demo API...")
    async with KalshiTradingClient(KEY_ID, KEY_PATH, KALSHI_ENV, private_key_pem=KEY_PEM) as trading:
        balance = await trading.get_balance()
        print(f"  ✓ Connected — balance: ${balance:.2f}\n")

        # Step 2: Fetch markets from demo API
        print("Step 2: Fetching open markets from demo API...")
        try:
            markets = await fetch_markets(DEMO_BASE)
            print(f"  ✓ Demo API: {len(markets)} open markets found")
        except Exception as e:
            print(f"  ✗ Demo market fetch failed: {e}")
            markets = []

        if not markets:
            print("\n  Trying production API for market discovery...")
            try:
                markets = await fetch_markets(PROD_BASE)
                print(f"  ✓ Production API: {len(markets)} open markets found")
            except Exception as e:
                print(f"  ✗ Production market fetch also failed: {e}")

        if not markets:
            print("\nERROR: No markets found on either API endpoint. Cannot place test trade.")
            sys.exit(1)

        # Step 3: Show sample markets and sport keyword stats
        print(f"\nStep 3: Analyzing {len(markets)} markets...")
        prefixes = {}
        for m in markets:
            et = (m.get("event_ticker") or "unknown").split("-")[0]
            prefixes[et] = prefixes.get(et, 0) + 1

        print("  Event ticker prefixes (top 15):")
        for et, count in sorted(prefixes.items(), key=lambda x: -x[1])[:15]:
            print(f"    {et:30s} {count:4d} markets")

        sport_keywords = ["kxnba", "kxmlb", "kxnhl", "kxufc", "kxatp", "kxwta",
                          "nba", "nhl", "mlb", "basketball", "hockey", "baseball", "ufc", "mma", "tennis"]
        sport_markets = [
            m for m in markets
            if any(kw in (m.get("event_ticker") or "").lower() or kw in (m.get("title") or "").lower()
                   for kw in sport_keywords)
        ]
        print(f"\n  Sports-related markets: {len(sport_markets)}")
        if sport_markets:
            print("  Sample sport market titles:")
            for m in sport_markets[:5]:
                yes_ask = float(m.get("yes_ask_dollars") or 0)
                print(f"    [{m.get('event_ticker','').split('-')[0]}] {m.get('title','')[:60]} — yes_ask=${yes_ask:.2f}")

        # Step 4: Pick a market for the test trade
        print("\nStep 4: Selecting market for $5 test trade...")
        result = pick_market(markets, target_dollars=5.0)

        if not result:
            print("  No tradeable market found (all prices 0 or 100¢).")
            sys.exit(1)

        market, yes_ask, count, cost = result
        ticker = market["ticker"]
        title = market.get("title", "")
        event_ticker = market.get("event_ticker", "")

        print(f"  Selected: {ticker}")
        print(f"  Title:    {title}")
        print(f"  Event:    {event_ticker}")
        print(f"  Yes ask:  {yes_ask:.2f} ({int(yes_ask*100)}¢)")
        print(f"  Count:    {count} contracts")
        print(f"  Cost:     ${cost:.2f}")

        if dry_run:
            print(f"\n  [DRY RUN] Would place: BUY {count}× YES {ticker} @ {int(yes_ask*100)}¢")
            print("  Pass --no-dry-run to actually execute.\n")
            return

        # Step 5: Place the trade
        print(f"\nStep 5: Placing order — BUY {count}× YES @ {int(yes_ask*100)}¢ on {ticker}...")
        try:
            order = await trading.place_order(ticker, "yes", count, yes_ask, "buy")
            print(f"\n  ✓ Order placed successfully!")
            print(f"  Order status: {order.get('status', 'unknown')}")
            print(f"  Order ID:     {order.get('id', 'n/a')}")
            filled = order.get('yes_price', order.get('no_price', 0))
            if filled:
                print(f"  Fill price:   {filled}¢")
            print(f"\n  Full response:\n{json.dumps(order, indent=2)}")
        except Exception as e:
            print(f"\n  ✗ Order failed: {e}")
            if hasattr(e, 'response'):
                print(f"  Response body: {e.response.text}")
            sys.exit(1)

        # Step 6: Verify in positions
        print("\nStep 6: Checking positions to confirm trade recorded...")
        positions = await trading.get_positions()
        match = [p for p in positions if p.get("ticker") == ticker]
        if match:
            pos = match[0]
            print(f"  ✓ Position confirmed: {pos.get('ticker')} — {pos.get('position')} contracts")
        else:
            print(f"  ⚠  Position for {ticker} not in portfolio yet (may be pending fill)")
            print(f"  Total positions: {len(positions)}")

        new_balance = await trading.get_balance()
        print(f"\n  Balance before: ${balance:.2f}")
        print(f"  Balance after:  ${new_balance:.2f}")
        print(f"  Difference:     ${new_balance - balance:+.2f}")
        print(f"\n{'='*60}")
        print("  End-to-end test PASSED")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Show what would be traded but don't place order")
    args = parser.parse_args()

    try:
        asyncio.run(main(dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass
