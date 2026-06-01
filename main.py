import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from rich.logging import RichHandler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(show_path=False)],
)
logger = logging.getLogger(__name__)

from alert import on_opportunity, run_telegram_bot, set_positions_store, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from dedup import Deduplicator
from kalshi_client import KalshiClient
from positions import PositionStore
from run_detector import RunDetector
from score_feed import ScoreFeed
from strategies import arbitrage
from strategies.momentum import MomentumStrategy

ARBITRAGE_POLL = float(os.getenv("ARBITRAGE_POLL_SECONDS", "30"))
ENTRY_POLL = float(os.getenv("ENTRY_POLL_SECONDS", "60"))


async def run_arbitrage_loop(kalshi: KalshiClient, dedup: Deduplicator):
    logger.info("Arbitrage monitor started (poll every %.0fs)", ARBITRAGE_POLL)
    while True:
        try:
            markets = await kalshi.get_sports_markets()
            for opp in arbitrage.detect(markets):
                if dedup.is_new(opp):
                    on_opportunity(opp)
        except Exception as e:
            logger.error("Arbitrage loop error: %s", e)
        await asyncio.sleep(ARBITRAGE_POLL)


async def run_entry_loop(kalshi: KalshiClient, momentum: MomentumStrategy, dedup: Deduplicator):
    logger.info("Entry signal scanner started (poll every %.0fs)", ENTRY_POLL)
    while True:
        try:
            markets = await kalshi.get_sports_markets()
            entries = await momentum.evaluate_entry(markets)
            for opp in entries:
                if dedup.is_new(opp):
                    on_opportunity(opp)
        except Exception as e:
            logger.error("Entry loop error: %s", e)
        await asyncio.sleep(ENTRY_POLL)


async def run_score_and_run_loop(
    kalshi: KalshiClient,
    momentum: MomentumStrategy,
    dedup: Deduplicator,
):
    logger.info("Score feed + run detection started")
    run_detector = RunDetector()

    async with ScoreFeed() as feed:
        async for score_event in feed.stream():
            logger.debug(
                "Score: %s %s %d → %d",
                score_event.sport.value, score_event.team,
                score_event.old_score, score_event.new_score,
            )

            run = run_detector.ingest(score_event)
            if run:
                try:
                    opp = await momentum.evaluate_exit(run)
                    if opp and dedup.is_new(opp):
                        on_opportunity(opp)
                except Exception as e:
                    logger.error("Exit eval error: %s", e)


async def main():
    dedup = Deduplicator()
    positions = PositionStore()
    set_positions_store(positions)

    async with KalshiClient() as kalshi:
        momentum = MomentumStrategy(kalshi, positions)

        logger.info("Monitoring Kalshi: NBA, MLB, NHL, UFC, Tennis")
        if positions.get_all():
            logger.info("Open positions: %d", len(positions.get_all()))

        tasks = [
            run_arbitrage_loop(kalshi, dedup),
            run_entry_loop(kalshi, momentum, dedup),
            run_score_and_run_loop(kalshi, momentum, dedup),
        ]

        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            logger.info("Telegram bot enabled — alerts go to chat %s", TELEGRAM_CHAT_ID)
            tasks.append(run_telegram_bot())
        else:
            logger.info("Telegram not configured — alerts print to console")

        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitor stopped.")
