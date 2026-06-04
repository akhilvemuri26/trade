"""
Parallel strategy tester.

Runs multiple strategy configurations simultaneously against the Kalshi demo API.
Each strategy auto-executes trades and tracks P&L independently.
Results auto-save every 15 minutes so nothing is lost on crash.

Usage:
    python test_strategies.py                    # run for 24 hours
    python test_strategies.py --duration 0       # run indefinitely (until Ctrl+C)
    python test_strategies.py --duration 7200    # run for 2 hours
"""

import argparse
import asyncio
import datetime
import logging
import os
import signal
import time

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(show_path=False)],
)
logger = logging.getLogger(__name__)

from kalshi_client import KalshiClient
from pnl import compute_portfolio_pnl
from score_feed import ScoreFeed
from strategy_config import STRATEGIES
from simulation_store import create_manifest, load_manifest, save_manifest, save_paper_state
from virtual_trader import VirtualTrader

console = Console()

AUTOSAVE_INTERVAL = 900  # 15 minutes
SETTLEMENT_INTERVAL = int(os.getenv("SETTLEMENT_INTERVAL", "300"))
EXIT_SCAN_INTERVAL = int(os.getenv("EXIT_SCAN_INTERVAL", "30"))


async def run_exit_scanner(
    kalshi: KalshiClient,
    traders: list[VirtualTrader],
    interval: float = EXIT_SCAN_INTERVAL,
):
    """Poll Kalshi prices and apply swing exit rules on open positions."""
    while True:
        try:
            markets = await kalshi.get_sports_markets()
            by_ticker = {m.ticker: m for m in markets}
            for trader in traders:
                await trader.check_exits(by_ticker, kalshi)
        except Exception as e:
            logger.error("Exit scan error: %s", e)
        await asyncio.sleep(interval)


async def run_settlement_scanner(
    kalshi: KalshiClient,
    traders: list[VirtualTrader],
    paper=None,
    interval: float = SETTLEMENT_INTERVAL,
):
    """Settle resolved positions against Kalshi market results."""
    if paper is None:
        return
    while True:
        try:
            total = 0
            for trader in traders:
                n = await trader.settle_open_positions(kalshi)
                total += n
            if total:
                logger.info("Settled %d position(s) across strategies", total)
        except Exception as e:
            logger.error("Settlement scan error: %s", e)
        await asyncio.sleep(interval)


async def run_entry_scanner(kalshi: KalshiClient, traders: list[VirtualTrader], poll_interval: float):
    while True:
        try:
            markets = await kalshi.get_sports_markets()
            for trader in traders:
                await trader.check_entries(markets, all_traders=traders)
            from market_view import write_snapshot
            # Scores every market under every gated strategy (CPU-bound);
            # offload so it doesn't block the event loop / health checks.
            await asyncio.to_thread(write_snapshot, markets)
        except Exception as e:
            logger.error("Entry scan error: %s", e)
        await asyncio.sleep(poll_interval)


async def run_score_feed(kalshi: KalshiClient, traders: list[VirtualTrader]):
    while True:
        try:
            async with ScoreFeed() as feed:
                async for event in feed.stream():
                    for trader in traders:
                        try:
                            await trader.process_score(event, kalshi)
                        except Exception as e:
                            logger.error("[%s] Score processing error: %s", trader.name, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Score feed crashed: %s — restarting in 15s", e)
            await asyncio.sleep(15)


async def run_autosave(traders: list[VirtualTrader], kalshi: KalshiClient):
    """Periodically save results so nothing is lost on crash."""
    for trader in traders:
        trader.save_results()
    logger.info("Initial save complete for %d strategies", len(traders))
    while True:
        await asyncio.sleep(AUTOSAVE_INTERVAL)
        for trader in traders:
            trader.save_results()
        manifest = load_manifest()
        if manifest:
            manifest["last_autosave"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            save_manifest(manifest)
        logger.info("Auto-saved results for %d strategies", len(traders))
        print_live_status(traders, kalshi)


def print_live_status(traders: list[VirtualTrader], kalshi: KalshiClient | None = None):
    """Print a compact live status update."""
    console.print()
    now = datetime.datetime.now().strftime("%H:%M:%S")
    console.rule(f"[dim]Live status — {now}[/dim]")
    if kalshi is not None:
        pnl = compute_portfolio_pnl(traders, kalshi, balance=0.0, initial_balance=10_000.0)
        net_color = "green" if pnl["portfolio_net_pl"] >= 0 else "red"
        console.print(
            f"  [bold]Portfolio[/bold]: "
            f"wallet closed ${pnl['wallet_realized_pl']:+.2f}, "
            f"unrealized ${pnl['unrealized_pl']:+.2f}, "
            f"[{net_color}]net ${pnl['portfolio_net_pl']:+.2f}[/{net_color}]"
        )
    for trader in traders:
        s = trader.summary()
        pl_color = "green" if s["realized_pl"] >= 0 else "red"
        console.print(
            f"  [bold]{s['name']}[/bold]: "
            f"{s['entries']} entries, {s['exits']} exits, "
            f"[{pl_color}]${s['realized_pl']:+.2f}[/{pl_color}] realized, "
            f"{s['open_positions']} open"
        )


def print_results(traders: list[VirtualTrader], elapsed: float):
    console.print()
    console.rule("[bold]Final Strategy Comparison")

    hours = elapsed / 3600
    mins = (elapsed % 3600) / 60
    if hours >= 1:
        console.print(f"Duration: {int(hours)}h {int(mins)}m\n")
    else:
        console.print(f"Duration: {elapsed / 60:.1f} minutes\n")

    table = Table(show_header=True, header_style="bold cyan", show_lines=True)
    table.add_column("Strategy", style="bold", min_width=22)
    table.add_column("Exit", min_width=8)
    table.add_column("Entries", justify="right")
    table.add_column("Exits", justify="right")
    table.add_column("W/L", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Realized P&L", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("ROI", justify="right")
    table.add_column("Open", justify="right")

    for trader in traders:
        s = trader.summary()
        c = trader._config
        pl_color = "green" if s["realized_pl"] >= 0 else "red"
        avg_color = "green" if s["avg_pl"] >= 0 else "red"
        roi_color = "green" if s["roi"] >= 0 else "red"

        table.add_row(
            s["name"],
            s.get("exit_mode", "?"),
            str(s["entries"]),
            str(s["exits"]),
            f"{s['wins']}/{s['losses']}",
            f"{s['win_rate']:.1f}%",
            f"[{pl_color}]${s['realized_pl']:+.2f}[/{pl_color}]",
            f"[{avg_color}]${s['avg_pl']:+.2f}[/{avg_color}]",
            f"[{roi_color}]{s['roi']:+.1f}%[/{roi_color}]",
            str(s["open_positions"]),
        )

    console.print(table)

    console.print("\n[bold]Results saved to:[/bold]")
    for trader in traders:
        path = trader.save_results()
        console.print(f"  trades + events: {path}")
    console.print(f"  detailed logs:   results/logs/")


async def main(duration: int):
    start = time.time()
    from paper_executor import PaperExecutor, set_persist_hooks
    from simulation_store import archive_and_reset_simulation, restore_paper_executor

    paper_balance = float(os.getenv("PAPER_BALANCE", "10000"))
    paper = PaperExecutor(paper_balance)

    if os.getenv("FRESH_EXPERIMENT", "").lower() in ("1", "true", "yes"):
        dest = archive_and_reset_simulation(paper, paper_balance, STRATEGIES)
        logger.info("Fresh experiment: archived to %s", dest)
        create_manifest(STRATEGIES, paper_balance)
    else:
        restore_paper_executor(paper)
        if not load_manifest():
            create_manifest(STRATEGIES, paper_balance)

    set_persist_hooks(
        lambda: save_paper_state(paper),
        lambda _extra: None,
    )

    async with KalshiClient() as kalshi:
        traders = [VirtualTrader(config, kalshi, paper=paper) for config in STRATEGIES]
        names = ", ".join(t.name for t in traders)
        logger.info("Starting %d strategies: %s", len(traders), names)

        if duration > 0:
            hours = duration / 3600
            logger.info("Running for %.1f hours (Ctrl+C to stop early)", hours)
        else:
            logger.info("Running indefinitely (Ctrl+C to stop)")

        stop = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        for trader in traders:
            await trader.settle_open_positions(kalshi)

        tasks = [
            asyncio.create_task(run_entry_scanner(kalshi, traders, poll_interval=60)),
            asyncio.create_task(run_exit_scanner(kalshi, traders)),
            asyncio.create_task(run_score_feed(kalshi, traders)),
            asyncio.create_task(run_autosave(traders, kalshi)),
            asyncio.create_task(run_settlement_scanner(kalshi, traders, paper=paper)),
        ]

        if duration > 0:
            async def timeout():
                await asyncio.sleep(duration)
                stop.set()
            tasks.append(asyncio.create_task(timeout()))

        await stop.wait()

        for t in tasks:
            t.cancel()

        elapsed = time.time() - start
        print_results(traders, elapsed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parallel strategy tester")
    parser.add_argument(
        "--duration", type=int, default=86400,
        help="Test duration in seconds. 0 = run forever. Default: 86400 (24 hours)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.duration))
    except KeyboardInterrupt:
        pass
