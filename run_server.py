"""
Paper trading server.

Runs paper executor (port 8100) + web dashboard (port 8080) + strategy tasks.
Uses production Kalshi API for real market data, simulates fills internally.
No credentials needed — read-only market data + in-memory paper fills.
"""

import asyncio
import datetime
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

from paths import RESULTS_DIR, ensure_data_dirs

if os.getenv("PLAIN_LOGS", "").lower() in ("1", "true", "yes") or os.getenv("FLY_APP_NAME"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
else:
    from rich.logging import RichHandler
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(show_path=False)],
    )
logger = logging.getLogger(__name__)

from kalshi_client import KalshiClient, SettlementInfo, market_side_labels, parse_settlement
from paper_executor import PaperExecutor
from strategy_config import STRATEGIES
from virtual_trader import VirtualTrader
from pnl import compute_portfolio_pnl, conservative_metrics
from simulation_store import (
    archive_and_reset_simulation,
    create_manifest,
    load_manifest,
    load_ledger_trades,
    load_runs_index,
    load_archived_run,
    load_archived_trades,
    mark_manifest_stopped,
    purge_orphan_positions,
    rebuild_strategy_trades_from_ledger,
    restore_paper_executor,
    save_manifest,
    save_paper_state,
)
from paper_executor import set_persist_hooks
from test_strategies import (
    run_entry_scanner,
    run_exit_scanner,
    run_score_feed,
    run_autosave,
    run_settlement_scanner,
    print_results,
)
from alert import (
    run_telegram_bot, run_hourly_digest,
    set_traders_ref,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)

DURATION = int(os.getenv("TEST_DURATION", "86400"))
PAPER_BALANCE = float(os.getenv("PAPER_BALANCE", "10000"))
DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"
PAGES_DIR = Path(__file__).parent / "pages"
STRATEGIES_HTML = PAGES_DIR / "strategies.html"
TRADES_HTML = PAGES_DIR / "trades.html"


def _enrich_side_labels(title: str, ticker: str, side: str) -> dict:
    labels = market_side_labels(title, ticker)
    selected = labels["yes_label"] if side == "yes" else labels["no_label"]
    return {
        "yes_label": labels["yes_label"],
        "no_label": labels["no_label"],
        "selected_team": selected,
        "selected_side": side,
        "yes_ticker_code": labels["yes_ticker_code"],
    }


def _normalize_trades(rows: list[dict]) -> list[dict]:
    out = []
    for t in rows:
        side = t.get("side", "yes")
        enriched = dict(t)
        enriched.update(_enrich_side_labels(t.get("title", ""), t.get("ticker", ""), side))
        ts = float(enriched.get("timestamp") or 0)
        dt = datetime.datetime.fromtimestamp(ts) if ts else None
        action = enriched.get("action")
        entry_price = enriched.get("entry_price")
        if entry_price is None and action == "buy":
            entry_price = enriched.get("price")
        exit_price = (
            enriched.get("exit_price")
            if enriched.get("exit_price") is not None
            else enriched.get("lock_price")
        )
        if exit_price is None and action == "settle":
            payout = enriched.get("payout")
            count = enriched.get("count") or 0
            exit_price = (payout / count) if payout is not None and count else None
        display_price = exit_price if exit_price is not None else entry_price
        enriched["entry_price_norm"] = entry_price
        enriched["exit_price_norm"] = exit_price
        enriched["display_price"] = display_price
        enriched["display_profit"] = enriched.get("profit")
        enriched["trade_datetime_iso"] = dt.isoformat() if dt else None
        enriched["trade_date"] = dt.date().isoformat() if dt else None
        enriched["trade_time"] = dt.time().strftime("%H:%M:%S") if dt else None
        out.append(enriched)
    return out


def _strategy_description(config: dict) -> str:
    mode = config.get("exit_mode", "hybrid")
    if mode == "sell":
        if config.get("min_sell_profit_usd"):
            return f"Sell YES when profit >= ${config['min_sell_profit_usd']:.2f}"
        return f"Sell YES when profit >= {config.get('min_sell_profit_pct', 0) * 100:.1f}%"
    if mode == "lock":
        if config.get("min_lock_profit_usd"):
            return f"Lock with NO when guaranteed profit >= ${config['min_lock_profit_usd']:.2f}"
        return f"Lock with NO when guaranteed profit >= {config.get('min_lock_profit_pct', 0) * 100:.1f}%"
    style = config.get("hybrid_style", "sell_first")
    sell = config.get("min_sell_profit_pct")
    lock_usd = config.get("min_lock_profit_usd")
    return (
        f"Hybrid ({style}): sell at {sell * 100:.1f}% profit, else lock at ${lock_usd:.2f}+"
        if sell is not None and lock_usd is not None
        else f"Hybrid ({style}) sell/lock thresholds"
    )


def _apply_trade_filters_and_sort(rows: list[dict], query: dict[str, str]) -> list[dict]:
    strategy = (query.get("strategy") or "").strip()
    action = (query.get("action") or "").strip().lower()
    ticker = (query.get("ticker") or "").strip().lower()
    date_from = (query.get("date_from") or "").strip()
    date_to = (query.get("date_to") or "").strip()
    min_pl = query.get("min_pl")
    max_pl = query.get("max_pl")
    anomalies_only = (query.get("anomalies_only") or "").strip().lower() in ("1", "true", "yes")

    out = rows
    if strategy:
        out = [r for r in out if r.get("strategy") == strategy]
    if action:
        out = [r for r in out if str(r.get("action", "")).lower() == action]
    if ticker:
        out = [r for r in out if ticker in str(r.get("ticker", "")).lower()]
    if date_from:
        out = [r for r in out if (r.get("trade_date") or "") >= date_from]
    if date_to:
        out = [r for r in out if (r.get("trade_date") or "") <= date_to]
    if min_pl is not None and min_pl != "":
        try:
            v = float(min_pl)
            out = [r for r in out if (r.get("display_profit") is not None and float(r["display_profit"]) >= v)]
        except ValueError:
            pass
    if max_pl is not None and max_pl != "":
        try:
            v = float(max_pl)
            out = [r for r in out if (r.get("display_profit") is not None and float(r["display_profit"]) <= v)]
        except ValueError:
            pass
    if anomalies_only:
        out = [r for r in out if r.get("anomaly_flag")]

    sort_key = (query.get("sort") or "timestamp").strip()
    order = (query.get("order") or "desc").strip().lower()
    reverse = order != "asc"

    def _key(item: dict[str, Any]):
        if sort_key in ("timestamp", "time", "date"):
            return float(item.get("timestamp") or 0)
        if sort_key in ("profit", "pl"):
            return float(item.get("display_profit") if item.get("display_profit") is not None else -10**12)
        if sort_key in ("price", "display_price"):
            return float(item.get("display_price") if item.get("display_price") is not None else -1)
        if sort_key in ("strategy", "action", "ticker", "team"):
            lookup = {
                "strategy": item.get("strategy", ""),
                "action": item.get("action", ""),
                "ticker": item.get("ticker", ""),
                "team": item.get("selected_team", ""),
            }
            return str(lookup.get(sort_key, "")).lower()
        return float(item.get("timestamp") or 0)

    out.sort(key=_key, reverse=reverse)
    return out


def _build_reconciliation(pnl: dict, positions: list[dict]) -> dict:
    expected = round(pnl["wallet_realized_pl"] + pnl["unrealized_pl"], 2)
    delta = round(pnl["portfolio_net_pl"] - expected, 2)
    marked = sum(1 for p in positions if p.get("current_price") is not None)
    unmarked = len(positions) - marked
    unmarked_cost = round(
        sum((p.get("entry_price") or 0) * (p.get("count") or 0) for p in positions if p.get("current_price") is None),
        2,
    )
    ok = abs(delta) <= 0.01 and unmarked == 0
    return {
        "ok": ok,
        "portfolio_net_pl": pnl["portfolio_net_pl"],
        "wallet_realized_pl": pnl["wallet_realized_pl"],
        "unrealized_pl": pnl["unrealized_pl"],
        "expected_net_pl": expected,
        "reconciliation_delta": delta,
        "marked_positions": marked,
        "unmarked_positions": unmarked,
        "unmarked_cost_basis": unmarked_cost,
    }


async def _resolution_integrity(kalshi: KalshiClient, limit: int = 400) -> dict:
    unresolved_mismatches = []
    settle_payout_mismatches = []
    lock_profit_mismatches = []
    suspicious_exits = []
    recent_rows = load_ledger_trades(limit)
    for row in recent_rows:
        action = row.get("action")
        if action == "settle":
            side = row.get("side")
            result = row.get("result")
            if side in ("yes", "no") and result in ("yes", "no") and side != result:
                if (row.get("profit") or 0) > 0:
                    unresolved_mismatches.append({
                        "ticker": row.get("ticker"),
                        "strategy": row.get("strategy"),
                        "profit": row.get("profit"),
                        "side": side,
                        "result": result,
                    })
            # Payout/profit arithmetic validation (strict settlement correctness).
            count = float(row.get("count") or 0)
            entry_price = float(row.get("entry_price") or 0)
            payout = row.get("payout")
            if payout is None:
                payout = float(row.get("price") or 0) * count
            payout = float(payout)
            expected = payout - (entry_price * count)
            profit = float(row.get("profit") or 0)
            if abs(round(expected - profit, 2)) > 0.01:
                settle_payout_mismatches.append({
                    "ticker": row.get("ticker"),
                    "strategy": row.get("strategy"),
                    "expected_profit": round(expected, 2),
                    "recorded_profit": round(profit, 2),
                    "delta": round(expected - profit, 2),
                })
        elif action == "lock":
            # A lock buys the opposite side to hedge, earning a guaranteed
            # $1/contract payout REGARDLESS of which side resolves. So the
            # resolved side is irrelevant -- comparing it to the hedge side
            # (the old check) flagged every won game as "suspicious". The only
            # thing that can be wrong is booking more than the lock guarantees:
            #   locked profit = (1 - entry - lock_price) * count.
            count = float(row.get("count") or 0)
            entry_price = float(row.get("entry_price") or 0)
            lock_price = row.get("lock_price")
            if lock_price is None:
                lock_price = row.get("exit_price")
            if lock_price is None:
                lock_price = row.get("price")
            lock_price = float(lock_price or 0)
            recorded = float(row.get("profit") or 0)
            max_lock_profit = (1.0 - entry_price - lock_price) * count
            if recorded - max_lock_profit > 0.01:
                lock_profit_mismatches.append({
                    "ticker": row.get("ticker"),
                    "strategy": row.get("strategy"),
                    "recorded_profit": round(recorded, 2),
                    "max_lock_profit": round(max_lock_profit, 2),
                    "delta": round(recorded - max_lock_profit, 2),
                })

    # Outright YES sells: flag a sell near $1 that resolved the other way (booked
    # a near-certain win that didn't happen). Locks are excluded -- their payoff
    # does not depend on the resolved side (validated arithmetically above).
    recent_sells = [r for r in recent_rows if r.get("action") == "sell"]
    resolution_cache: dict[str, Optional[SettlementInfo]] = {}
    for row in recent_sells:
        ticker = row.get("ticker")
        if ticker and ticker not in resolution_cache:
            raw = await kalshi.get_market_raw(ticker)
            resolution_cache[ticker] = parse_settlement(raw) if raw else None
    for row in recent_sells:
        ticker = row.get("ticker")
        info = resolution_cache.get(ticker) if ticker else None
        if not info:
            continue
        side = row.get("side", "yes")
        px = float(row.get("exit_price") or row.get("price") or 0)
        if px >= 0.99 and side in ("yes", "no") and side != info.result:
            suspicious_exits.append({
                "ticker": ticker,
                "strategy": row.get("strategy"),
                "action": "sell",
                "exit_price": px,
                "side": side,
                "resolved_side": info.result,
                "resolution_source": info.resolution_source,
                "profit": round(float(row.get("profit") or 0), 2),
            })
    suspicious_positive_profit = round(
        sum(max(0.0, float(x.get("profit") or 0.0)) for x in suspicious_exits), 2,
    )
    return {
        "settlement_mismatches": unresolved_mismatches,
        "settle_payout_mismatches": settle_payout_mismatches,
        "lock_profit_mismatches": lock_profit_mismatches,
        "suspicious_exits": suspicious_exits,
        "suspicious_positive_profit": suspicious_positive_profit,
    }


async def run_executor_server(paper: PaperExecutor, ready: asyncio.Event | None = None):
    """Paper executor HTTP server on port 8100 (localhost only)."""

    async def handle_buy(req):
        b = await req.json()
        return web.json_response(
            paper.buy(
                b["ticker"], b.get("side", "yes"), b["count"], b["price"],
                strategy=b.get("strategy", ""), title=b.get("title", ""),
            )
        )

    async def handle_sell(req):
        b = await req.json()
        return web.json_response(
            paper.sell(
                b["ticker"], b["side"], b["count"], b["price"],
                strategy=b.get("strategy", ""), title=b.get("title", ""),
            )
        )

    async def handle_lock(req):
        b = await req.json()
        return web.json_response(
            paper.lock(
                b["ticker"], b["side"], b["count"], b["price"],
                strategy=b.get("strategy", ""), title=b.get("title", ""),
            )
        )

    async def handle_health(req):
        return web.json_response({
            "status": "ok",
            "env": "paper",
            "balance": paper.get_balance(),
        })

    app = web.Application()
    app.router.add_post("/order/buy", handle_buy)
    app.router.add_post("/order/sell", handle_sell)
    app.router.add_post("/order/lock", handle_lock)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8100)
    try:
        await site.start()
    except OSError as e:
        logger.error(
            "Paper executor failed to bind port 8100: %s. "
            "Stop executor/server.py if it is already using this port.",
            e,
        )
        raise
    logger.info("Paper executor running on localhost:8100 (balance: $%.2f)", paper.get_balance())
    if ready is not None:
        ready.set()
    await asyncio.Event().wait()


async def run_dashboard_server(
    paper: PaperExecutor,
    traders: list[VirtualTrader],
    kalshi: KalshiClient,
    start_time: float,
):
    """Public dashboard HTTP server on port 8080."""

    async def handle_index(req):
        if DASHBOARD_HTML.exists():
            return web.Response(
                body=DASHBOARD_HTML.read_bytes(),
                content_type="text/html",
            )
        return web.Response(text="Dashboard not found", status=404)

    async def handle_strategies_page(req):
        if STRATEGIES_HTML.exists():
            return web.Response(
                body=STRATEGIES_HTML.read_bytes(),
                content_type="text/html",
            )
        return web.Response(text="Strategies page not found", status=404)

    async def handle_trades_page(req):
        if TRADES_HTML.exists():
            return web.Response(
                body=TRADES_HTML.read_bytes(),
                content_type="text/html",
            )
        return web.Response(text="Trades page not found", status=404)

    async def handle_api_status(req):
        now = time.time()
        manifest = load_manifest()
        strategies = []
        all_positions = []

        strategy_docs = []
        summary_by_name = {}
        for trader in traders:
            s = trader.summary()
            strategies.append(s)
            summary_by_name[s["name"]] = s
            for pos in trader._positions.get_all():
                current_price = None
                mark_source = "missing"
                for m in kalshi._cache:
                    if m.ticker == pos.ticker:
                        if pos.side == "yes":
                            if m.yes_bid > 0:
                                current_price = m.yes_bid
                                mark_source = "yes_bid"
                        else:
                            if m.no_bid > 0:
                                current_price = m.no_bid
                                mark_source = "no_bid"
                        break
                all_positions.append({
                    "ticker": pos.ticker,
                    "title": pos.title,
                    "sport": pos.sport,
                    "strategy": trader.name,
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "count": pos.count,
                    "current_price": current_price,
                    "mark_source": mark_source,
                    "unrealized_pl": (
                        round((current_price - pos.entry_price) * pos.count, 2)
                        if current_price is not None else None
                    ),
                    "hold_seconds": round(now - pos.timestamp, 0),
                    **_enrich_side_labels(pos.title, pos.ticker, pos.side),
                })

        initial = paper.get_initial_balance()
        trade_log = paper.get_trade_log()
        pnl = compute_portfolio_pnl(
            traders, kalshi, paper.get_balance(), initial, trade_log,
        )
        integrity = await _resolution_integrity(kalshi, limit=500)
        conservative = conservative_metrics(
            wallet_realized_pl=pnl["wallet_realized_pl"],
            unrealized_pl=pnl["unrealized_pl"],
            portfolio_net_pl=pnl["portfolio_net_pl"],
            suspicious_positive_profit=integrity["suspicious_positive_profit"],
        )
        total_entries = sum(s["entries"] for s in strategies)
        total_exits = sum(s["exits"] for s in strategies)
        total_wins = sum(s["wins"] for s in strategies)

        all_trades = []
        for trader in traders:
            for t in reversed(trader._trades):
                all_trades.append({**t, "strategy": trader.name})
        all_trades.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
        if not all_trades:
            all_trades = load_ledger_trades(800)
        all_trades = _normalize_trades(all_trades)

        top_markets = [
            {
                "ticker": m.ticker,
                "title": m.title,
                "sport": m.sport,
                "volume": m.volume,
                "yes_ask": m.yes_ask,
                "yes_bid": m.yes_bid,
                "no_ask": m.no_ask,
                "no_bid": m.no_bid,
            }
            for m in sorted(kalshi._cache, key=lambda x: x.volume, reverse=True)[:15]
        ]

        ranked = sorted(
            strategies,
            key=lambda s: s.get("realized_pl", 0),
            reverse=True,
        )
        for config in STRATEGIES:
            live = summary_by_name.get(config["name"], {})
            strategy_docs.append({
                "name": config["name"],
                "description": _strategy_description(config),
                "config": config,
                "live": {
                    "entries": live.get("entries", 0),
                    "exits": live.get("exits", 0),
                    "open_positions": live.get("open_positions", 0),
                    "realized_pl": live.get("realized_pl", 0),
                    "win_rate": live.get("win_rate", 0),
                    "roi": live.get("roi", 0),
                },
            })

        reconciliation = _build_reconciliation(pnl, all_positions)
        reconciliation.update(integrity)
        reconciliation.update(conservative)
        reconciliation["ok"] = (
            reconciliation.get("ok", False)
            and abs(reconciliation["conservative_reconciliation_delta"]) <= 0.01
        )

        return web.json_response({
            "simulation_run_id": manifest.get("run_id") if manifest else None,
            "experiment_started_at": manifest.get("started_at") if manifest else None,
            "experiment_status": manifest.get("status") if manifest else None,
            "strategy_leaderboard": ranked,
            "balance": paper.get_balance(),
            "initial_balance": initial,
            "portfolio_net_pl": conservative["conservative_portfolio_net_pl"],
            "wallet_realized_pl": conservative["conservative_wallet_realized_pl"],
            "raw_portfolio_net_pl": pnl["portfolio_net_pl"],
            "raw_wallet_realized_pl": pnl["wallet_realized_pl"],
            "virtual_realized_sum": pnl["virtual_realized_sum"],
            "realized_pl": conservative["conservative_wallet_realized_pl"],
            "settled_pl": pnl["settled_pl"],
            "traded_pl": pnl["traded_pl"],
            "unrealized_pl": pnl["unrealized_pl"],
            "net_pl": conservative["conservative_portfolio_net_pl"],
            "equity": pnl["equity"],
            "open_market_value": pnl["open_market_value"],
            "open_cost_basis": pnl["open_cost_basis"],
            "cash_deployed": pnl["cash_deployed"],
            "expected_net_pl": conservative["conservative_expected_net_pl"],
            "reconciliation_delta": conservative["conservative_reconciliation_delta"],
            "marked_positions": pnl["marked_positions"],
            "unmarked_positions": pnl["unmarked_positions"],
            "unmarked_cost_basis": pnl["unmarked_cost_basis"],
            "total_pl": conservative["conservative_wallet_realized_pl"],
            "suspicious_positive_profit": conservative["suspicious_positive_profit"],
            "pnl_explanation": (
                "Conservative mode: report the lower valid P&L outcome. "
                "When resolution integrity is uncertain, suspicious positive exits "
                "are deducted from wallet-realized metrics."
            ),
            "reconciliation": reconciliation,
            "uptime_seconds": round(now - start_time, 0),
            "total_entries": total_entries,
            "total_exits": total_exits,
            "total_wins": total_wins,
            "win_rate": round(total_wins / total_exits * 100, 1) if total_exits > 0 else 0,
            "strategies": strategies,
            "strategy_docs": strategy_docs,
            "open_positions": all_positions,
            "recent_trades": _normalize_trades(trade_log[-100:]),
            "all_trades": all_trades[:200],
            "ledger_trades": load_ledger_trades(300),
            "top_markets_by_volume": top_markets,
            "markets_cached": len(kalshi._cache),
            "timestamp": now,
        })

    async def handle_api_trades(req):
        limit = min(int(req.query.get("limit", "500")), 2000)
        rows = []
        for trader in traders:
            for t in trader._trades:
                rows.append({**t, "strategy": trader.name})
        if not rows:
            rows = list(reversed(load_ledger_trades(5000)))
        rows = _normalize_trades(rows)
        integrity = await _resolution_integrity(kalshi, limit=1500)
        flagged = set(
            (
                x.get("ticker"),
                x.get("strategy"),
                x.get("action"),
            )
            for x in integrity.get("suspicious_exits", [])
        )
        payout_flagged = set(
            (
                x.get("ticker"),
                x.get("strategy"),
                "settle",
            )
            for x in integrity.get("settle_payout_mismatches", [])
        )
        lock_flagged = set(
            (
                x.get("ticker"),
                x.get("strategy"),
                "lock",
            )
            for x in integrity.get("lock_profit_mismatches", [])
        )
        flagged |= payout_flagged
        flagged |= lock_flagged
        for r in rows:
            r["anomaly_flag"] = (r.get("ticker"), r.get("strategy"), r.get("action")) in flagged
        filtered = _apply_trade_filters_and_sort(rows, req.query)
        return web.json_response({
            "trades": filtered[:limit],
            "paper_log": _normalize_trades(paper.get_trade_log()[-limit:]),
            "count": len(filtered),
            "total_count": len(rows),
        })

    async def handle_api_strategies(req):
        live = {t.name: t.summary() for t in traders}
        docs = []
        for config in STRATEGIES:
            s = live.get(config["name"], {})
            docs.append({
                "name": config["name"],
                "description": _strategy_description(config),
                "config": config,
                "live": {
                    "entries": s.get("entries", 0),
                    "exits": s.get("exits", 0),
                    "realized_pl": s.get("realized_pl", 0),
                    "open_positions": s.get("open_positions", 0),
                    "win_rate": s.get("win_rate", 0),
                    "roi": s.get("roi", 0),
                },
            })
        return web.json_response({"strategies": docs, "count": len(docs)})

    async def handle_api_reconcile(req):
        now = time.time()
        positions = []
        for trader in traders:
            for pos in trader._positions.get_all():
                cp = None
                for m in kalshi._cache:
                    if m.ticker == pos.ticker:
                        if pos.side == "yes":
                            cp = m.yes_bid if m.yes_bid > 0 else None
                        else:
                            cp = m.no_bid if m.no_bid > 0 else None
                        break
                positions.append({
                    "ticker": pos.ticker,
                    "strategy": trader.name,
                    "entry_price": pos.entry_price,
                    "count": pos.count,
                    "current_price": cp,
                })
        pnl = compute_portfolio_pnl(
            traders, kalshi, paper.get_balance(), paper.get_initial_balance(),
            paper.get_trade_log(),
        )
        recon = _build_reconciliation(pnl, positions)
        integrity = await _resolution_integrity(kalshi, limit=1000)
        conservative = conservative_metrics(
            wallet_realized_pl=pnl["wallet_realized_pl"],
            unrealized_pl=pnl["unrealized_pl"],
            portfolio_net_pl=pnl["portfolio_net_pl"],
            suspicious_positive_profit=integrity["suspicious_positive_profit"],
        )
        recon.update(integrity)
        recon.update(conservative)
        recon["timestamp"] = now
        return web.json_response(recon)

    async def handle_api_runs(req):
        runs = load_runs_index()
        manifest = load_manifest()
        if manifest:
            current_id = manifest.get("run_id")
            if not any(r["run_id"] == current_id for r in runs):
                runs.append({
                    "run_number": manifest.get("run_number", len(runs) + 1),
                    "run_id": current_id,
                    "label": manifest.get("run_label", f"Run {manifest.get('run_number', '?')}"),
                    "started_at": manifest.get("started_at"),
                    "ended_at": None,
                    "status": "running",
                    "initial_balance": manifest.get("paper_initial_balance"),
                    "archive_path": None,
                    "final_portfolio": None,
                })
        runs.sort(key=lambda r: r.get("run_number", 0))
        return web.json_response({"runs": runs, "current_run_id": manifest.get("run_id") if manifest else None})

    async def handle_api_run_status(req):
        run_id = req.match_info["run_id"]
        runs = load_runs_index()
        run_entry = next((r for r in runs if r["run_id"] == run_id), None)
        if not run_entry:
            return web.json_response({"error": "Run not found"}, status=404)
        archive_path = run_entry.get("archive_path")
        if not archive_path:
            return web.json_response({"error": "Run has no archive"}, status=404)
        archived = load_archived_run(archive_path)
        if not archived:
            return web.json_response({"error": "Archive data not found"}, status=404)
        manifest = archived.get("manifest", {})
        paper_state = archived.get("paper_state", {})
        final_portfolio = run_entry.get("final_portfolio") or manifest.get("final_portfolio", {})
        balance = paper_state.get("balance", 0)
        initial_balance = paper_state.get("initial_balance", run_entry.get("initial_balance", 0))
        trades = load_archived_trades(archive_path)
        trade_log = paper_state.get("trade_log", [])
        wallet_realized = sum(t.get("profit", 0) for t in trade_log if t.get("profit") is not None)
        total_entries = sum(1 for t in trades if t.get("action") == "buy")
        total_exits = sum(1 for t in trades if t.get("action") in ("sell", "lock", "settle"))
        total_wins = sum(1 for t in trades if t.get("action") in ("sell", "lock", "settle") and (t.get("profit") or 0) > 0)
        all_trades = _normalize_trades(trades)
        strategies_map: dict[str, dict] = {}
        for t in trades:
            name = t.get("strategy", "unknown")
            if name not in strategies_map:
                strategies_map[name] = {"name": name, "entries": 0, "exits": 0, "wins": 0, "losses": 0, "realized_pl": 0.0, "open_positions": 0, "exit_mode": "", "sell_exits": 0, "lock_exits": 0, "settle_exits": 0, "roi": 0, "profit_per_entry": 0, "win_rate": 0}
            s = strategies_map[name]
            action = t.get("action", "")
            if action == "buy":
                s["entries"] += 1
            elif action in ("sell", "lock", "settle"):
                s["exits"] += 1
                profit = t.get("profit", 0) or 0
                s["realized_pl"] += profit
                if profit > 0:
                    s["wins"] += 1
                else:
                    s["losses"] += 1
                if action == "sell":
                    s["sell_exits"] += 1
                elif action == "lock":
                    s["lock_exits"] += 1
                elif action == "settle":
                    s["settle_exits"] += 1
        for s in strategies_map.values():
            s["realized_pl"] = round(s["realized_pl"], 2)
            if s["exits"] > 0:
                s["win_rate"] = round(s["wins"] / s["exits"] * 100, 1)
            if s["entries"] > 0:
                s["profit_per_entry"] = round(s["realized_pl"] / s["entries"], 2)
        ranked = sorted(strategies_map.values(), key=lambda s: s["realized_pl"], reverse=True)
        return web.json_response({
            "simulation_run_id": manifest.get("run_id", run_id),
            "run_number": run_entry.get("run_number"),
            "run_label": run_entry.get("label"),
            "experiment_started_at": manifest.get("started_at", run_entry.get("started_at")),
            "experiment_status": "archived",
            "balance": balance,
            "initial_balance": initial_balance,
            "portfolio_net_pl": final_portfolio.get("portfolio_net_pl", round(balance - initial_balance, 2)),
            "wallet_realized_pl": round(wallet_realized, 2),
            "unrealized_pl": 0,
            "equity": balance,
            "open_market_value": 0,
            "open_cost_basis": 0,
            "cash_deployed": round(initial_balance - balance, 2),
            "total_entries": total_entries,
            "total_exits": total_exits,
            "total_wins": total_wins,
            "win_rate": round(total_wins / total_exits * 100, 1) if total_exits > 0 else 0,
            "strategy_leaderboard": ranked,
            "strategies": ranked,
            "open_positions": [],
            "all_trades": all_trades[-200:],
            "recent_trades": _normalize_trades(trade_log[-100:]),
            "top_markets_by_volume": [],
            "markets_cached": 0,
            "uptime_seconds": 0,
            "virtual_realized_sum": 0,
            "reconciliation": {"ok": True},
            "suspicious_positive_profit": 0,
            "timestamp": time.time(),
        })

    async def handle_api_run_trades(req):
        run_id = req.match_info["run_id"]
        runs = load_runs_index()
        run_entry = next((r for r in runs if r["run_id"] == run_id), None)
        if not run_entry:
            return web.json_response({"error": "Run not found"}, status=404)
        archive_path = run_entry.get("archive_path")
        if not archive_path:
            return web.json_response({"error": "Run has no archive"}, status=404)
        limit = min(int(req.query.get("limit", "500")), 5000)
        rows = load_archived_trades(archive_path, limit=10000)
        rows = _normalize_trades(rows)
        filtered = _apply_trade_filters_and_sort(rows, req.query)
        return web.json_response({
            "trades": filtered[:limit],
            "count": len(filtered),
            "total_count": len(rows),
        })

    async def handle_health(req):
        return web.json_response({"status": "ok", "service": "dashboard"})

    async def handle_api_market_scores(req):
        from market_view import read_snapshot
        return web.json_response(read_snapshot())

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/strategies", handle_strategies_page)
    app.router.add_get("/trades", handle_trades_page)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_get("/api/market-scores", handle_api_market_scores)
    app.router.add_get("/api/trades", handle_api_trades)
    app.router.add_get("/api/strategies", handle_api_strategies)
    app.router.add_get("/api/reconcile", handle_api_reconcile)
    app.router.add_get("/api/runs", handle_api_runs)
    app.router.add_get("/api/runs/{run_id}/status", handle_api_run_status)
    app.router.add_get("/api/runs/{run_id}/trades", handle_api_run_trades)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Dashboard running on http://0.0.0.0:8080")
    await asyncio.Event().wait()


def _init_simulation(paper: PaperExecutor) -> dict:
    active_names = {s["name"] for s in STRATEGIES}
    fresh = os.getenv("FRESH_EXPERIMENT", "").lower() in ("1", "true", "yes")

    if fresh:
        dest = archive_and_reset_simulation(paper, PAPER_BALANCE, STRATEGIES)
        logger.info("Fresh experiment: archived prior data to %s", dest)
        manifest = create_manifest(STRATEGIES, PAPER_BALANCE)
    else:
        removed = purge_orphan_positions(active_names)
        if removed:
            logger.info("Removed orphan position files: %s", removed)
        if restore_paper_executor(paper):
            logger.info(
                "Restored paper wallet balance=$%.2f (%d trades)",
                paper.get_balance(), len(paper.get_trade_log()),
            )
        manifest = load_manifest()
        if not manifest:
            manifest = create_manifest(STRATEGIES, PAPER_BALANCE)
        else:
            logger.info("Resuming simulation run_id=%s", manifest.get("run_id"))

    set_persist_hooks(
        lambda: save_paper_state(paper),
        lambda _extra: None,
    )
    save_paper_state(paper)
    return manifest


async def main():
    ensure_data_dirs()
    start = time.time()
    paper = PaperExecutor(PAPER_BALANCE)
    manifest = _init_simulation(paper)

    if os.getenv("FLY_APP_NAME"):
        logger.info(
            "Fly.io app=%s region=%s — dashboard public on :8080, data dir=%s",
            os.getenv("FLY_APP_NAME"),
            os.getenv("FLY_REGION", "?"),
            os.getenv("DATA_DIR", "project root"),
        )

    async with KalshiClient() as kalshi:
        traders = [VirtualTrader(config, kalshi, paper=paper) for config in STRATEGIES]
        replayed = rebuild_strategy_trades_from_ledger(traders)
        replay_total = sum(replayed.values())
        if replay_total:
            logger.info("Replayed %d strategy trade rows from ledger", replay_total)
        set_traders_ref(traders)
        logger.info(
            "Starting %d strategies (paper trading, $%.0f balance) — "
            "observe: Kalshi API | execute: PaperExecutor | display: :8080",
            len(traders), PAPER_BALANCE,
        )

        stop = asyncio.Event()
        executor_ready = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            logger.warning("Signal handlers unavailable — use SIGKILL to stop")

        def _log_task_exception(task: asyncio.Task):
            if not task.cancelled() and task.exception():
                logger.error("Task %s failed: %s", task.get_name(), task.exception())

        tasks = [
            asyncio.create_task(
                run_executor_server(paper, executor_ready), name="executor",
            ),
            asyncio.create_task(
                run_dashboard_server(paper, traders, kalshi, start),
                name="dashboard",
            ),
        ]

        await executor_ready.wait()
        logger.info("Executor ready — starting market scanners")

        markets = await kalshi.get_sports_markets()
        logger.info("Initial market load: %d moneyline markets cached", len(markets))

        for trader in traders:
            n = await trader.settle_open_positions(kalshi)
            if n:
                logger.info("[%s] Startup settlement: %d position(s)", trader.name, n)

        tasks.extend([
            asyncio.create_task(run_entry_scanner(kalshi, traders, poll_interval=60)),
            asyncio.create_task(run_exit_scanner(kalshi, traders)),
            asyncio.create_task(run_score_feed(kalshi, traders)),
            asyncio.create_task(run_autosave(traders, kalshi)),
            asyncio.create_task(run_settlement_scanner(kalshi, traders, paper=paper)),
        ])

        for t in tasks:
            t.add_done_callback(_log_task_exception)

        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            logger.info("Telegram bot enabled")
            tasks.append(asyncio.create_task(run_telegram_bot()))
            tasks.append(asyncio.create_task(
                run_hourly_digest(traders, start, paper=paper, kalshi=kalshi),
            ))

        if DURATION > 0:
            async def timeout():
                await asyncio.sleep(DURATION)
                stop.set()
            tasks.append(asyncio.create_task(timeout()))
            logger.info("Running for %d hours", DURATION // 3600)
        else:
            logger.info("Running indefinitely")

        await stop.wait()

        for t in tasks:
            t.cancel()

        elapsed = time.time() - start
        for trader in traders:
            trader.save_results()
        save_paper_state(paper)
        pnl = compute_portfolio_pnl(
            traders, kalshi, paper.get_balance(),
            paper.get_initial_balance(), paper.get_trade_log(),
        )
        mark_manifest_stopped(final_pl=pnl)
        _write_run_summary(paper, traders, elapsed, manifest)
        print_results(traders, elapsed)


def _write_run_summary(
    paper: PaperExecutor,
    traders: list,
    elapsed: float,
    manifest: dict | None = None,
):
    """Single combined snapshot for post-run analysis."""
    ensure_data_dirs()
    out_dir = RESULTS_DIR
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"run_summary_{ts}.json"
    payload = {
        "simulation_run_id": manifest.get("run_id") if manifest else None,
        "experiment": manifest.get("experiment") if manifest else None,
        "ended_at": datetime.datetime.now().isoformat(),
        "duration_seconds": round(elapsed, 1),
        "paper_balance": paper.get_balance(),
        "paper_initial_balance": paper.get_initial_balance(),
        "trade_log": paper.get_trade_log(),
        "strategies": [t.summary() for t in traders],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Run summary saved to %s", path)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
