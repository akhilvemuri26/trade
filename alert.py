import asyncio
import datetime
import logging
import os
import time
import uuid
from typing import Optional

import httpx

from models import Opportunity, OpportunityType, OrderResult, Position

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
EXECUTOR_URL = os.getenv("EXECUTOR_URL", "http://localhost:8100")
HEALTH_TIMEOUT = 15.0  # external Kalshi executor can be slow
PENDING_TTL = 300

_pending: dict[str, tuple[Opportunity, float]] = {}
_queue: asyncio.Queue[Opportunity] = asyncio.Queue()
_positions_store = None  # set by main.py
_traders_ref: list = []  # set by run_server to access VirtualTrader instances


def set_positions_store(store):
    global _positions_store
    _positions_store = store


def set_traders_ref(traders: list):
    global _traders_ref
    _traders_ref = traders


def _format_entry(opp: Opportunity) -> str:
    ts = datetime.datetime.fromtimestamp(opp.timestamp).strftime("%H:%M:%S")
    cost = opp.market.yes_ask * opp.suggested_count
    return (
        f"📈 *ENTRY SIGNAL* • {opp.market.sport} • {ts}\n\n"
        f"*{opp.market.title}*\n"
        f"{opp.signal}\n"
        f"Suggested: Buy {opp.suggested_count}× {opp.suggested_side.upper()} "
        f"at ${opp.market.yes_ask:.2f} (${cost:.2f} risk)"
    )


def _format_exit(opp: Opportunity) -> str:
    ts = datetime.datetime.fromtimestamp(opp.timestamp).strftime("%H:%M:%S")
    pos = opp.position
    if pos.side == "yes":
        current = opp.market.yes_ask
    else:
        current = opp.market.no_ask

    return (
        f"🔥 *RUN DETECTED* • {opp.market.sport} • {ts}\n\n"
        f"*{opp.market.title}*\n"
        f"{opp.signal}\n\n"
        f"Your position: {pos.count}× {pos.side.upper()} @ ${pos.entry_price:.2f}\n"
        f"Current price: ${current:.2f} → "
        f"unrealized {'+' if opp.sell_profit >= 0 else ''}"
        f"${opp.sell_profit:.2f}"
    )


def _format_arbitrage(opp: Opportunity) -> str:
    ts = datetime.datetime.fromtimestamp(opp.timestamp).strftime("%H:%M:%S")
    return (
        f"⚖️ *ARBITRAGE* • {opp.market.sport} • {ts}\n\n"
        f"*{opp.market.title}*\n"
        f"YES: ${opp.market.yes_ask:.2f} | NO: ${opp.market.no_ask:.2f} | "
        f"Sum: ${opp.market.yes_ask + opp.market.no_ask:.2f}\n"
        f"*Edge: {opp.edge * 100:.1f}% risk-free*"
    )


def _format_message(opp: Opportunity) -> str:
    if opp.type == OpportunityType.ENTRY:
        return _format_entry(opp)
    elif opp.type == OpportunityType.EXIT:
        return _format_exit(opp)
    else:
        return _format_arbitrage(opp)


def _evict_stale():
    now = time.time()
    expired = [k for k, (_, ts) in _pending.items() if now - ts > PENDING_TTL]
    for k in expired:
        del _pending[k]


def on_opportunity(opp: Opportunity) -> None:
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _queue.put_nowait(opp)
    else:
        _console_fallback(opp)


def _console_fallback(opp: Opportunity):
    from rich.console import Console
    from rich.panel import Panel
    console = Console()
    console.print(Panel(_format_message(opp).replace("*", ""), title=opp.type.value.upper()))


async def send_telegram(text: str, parse_mode: str = "Markdown"):
    """Send a plain text message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
            })
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def _sum_unrealized_pl(traders: list, kalshi) -> float:
    """Match dashboard open-position unrealized P&L."""
    total = 0.0
    for trader in traders:
        for pos in trader._positions.get_all():
            current_price = None
            for m in kalshi._cache:
                if m.ticker == pos.ticker:
                    current_price = m.yes_ask if pos.side == "yes" else m.no_ask
                    break
            if current_price is not None:
                total += (current_price - pos.entry_price) * pos.count
    return round(total, 2)


async def _check_executor_health_http() -> tuple[bool, float, list[str]]:
    """HTTP health probe for an external executor (not in-process paper)."""
    issues: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            resp = await client.get(f"{EXECUTOR_URL}/health")
            resp.raise_for_status()
            data = resp.json()
            return True, float(data.get("balance", 0)), issues
    except Exception as e:
        issues.append(f"Executor health check failed: {e}")
        return False, 0.0, issues


async def _build_digest(
    traders: list,
    start_time: float,
    *,
    paper=None,
    kalshi=None,
    is_startup: bool = False,
) -> str:
    """Build the status digest message (aligned with dashboard /api/status)."""
    now = time.time()
    uptime_h = (now - start_time) / 3600
    issues: list[str] = []

    if paper is not None:
        balance = paper.get_balance()
        executor_status = "Paper (in-process) ✅"
    else:
        executor_ok, balance, issues = await _check_executor_health_http()
        executor_status = "Running ✅" if executor_ok else "Error ❌"

    total_entries = 0
    total_exits = 0
    open_positions = 0

    for trader in traders:
        s = trader.summary()
        total_entries += s["entries"]
        total_exits += s["exits"]
        open_positions += s["open_positions"]

    wallet_realized = 0.0
    portfolio_net = 0.0
    unrealized_pl = None
    if paper is not None and kalshi is not None:
        from pnl import compute_portfolio_pnl
        pnl = compute_portfolio_pnl(
            traders, kalshi, balance,
            paper.get_initial_balance(), paper.get_trade_log(),
        )
        wallet_realized = pnl["wallet_realized_pl"]
        unrealized_pl = pnl["unrealized_pl"]
        portfolio_net = pnl["portfolio_net_pl"]
    elif kalshi:
        unrealized_pl = _sum_unrealized_pl(traders, kalshi)
        portfolio_net = unrealized_pl

    if not issues:
        issues_str = "None"
    else:
        issues_str = "\n".join(issues)

    pl_emoji = "🟢" if wallet_realized >= 0 else "🔴"
    title = "Startup Status" if is_startup else "Hourly Status"

    pl_line = f"*Wallet realized:* {pl_emoji} ${wallet_realized:+.2f}"
    if unrealized_pl is not None:
        ur_emoji = "🟢" if unrealized_pl >= 0 else "🔴"
        pl_line += f"\n*Unrealized:* {ur_emoji} ${unrealized_pl:+.2f}"
        net_emoji = "🟢" if portfolio_net >= 0 else "🔴"
        pl_line += f"\n*Portfolio net:* {net_emoji} ${portfolio_net:+.2f}"
    pl_line += f"\n*Balance:* ${balance:.2f}"

    msg = (
        f"⏰ *{title}* — {uptime_h:.1f}h uptime\n\n"
        f"*Executor:* {executor_status}\n"
        f"*Issues:* {issues_str}\n"
        f"{pl_line}\n"
        f"*Entries:* {total_entries} | *Exits:* {total_exits} | *Open:* {open_positions}\n"
    )

    return msg


async def run_hourly_digest(
    traders: list,
    start_time: float,
    *,
    paper=None,
    kalshi=None,
):
    """Send a startup message immediately, then hourly status digests."""
    DIGEST_INTERVAL = 3600

    await asyncio.sleep(5)
    try:
        msg = await _build_digest(
            traders, start_time, paper=paper, kalshi=kalshi, is_startup=True,
        )
        await send_telegram(msg)
        logger.info("Startup digest sent")
    except Exception as e:
        logger.error("Startup digest failed: %s", e)

    while True:
        await asyncio.sleep(DIGEST_INTERVAL)
        try:
            msg = await _build_digest(
                traders, start_time, paper=paper, kalshi=kalshi, is_startup=False,
            )
            await send_telegram(msg)
            logger.info("Hourly digest sent")
        except Exception as e:
            logger.error("Hourly digest failed: %s", e)


async def _call_executor(endpoint: str, payload: dict) -> OrderResult:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{EXECUTOR_URL}{endpoint}", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return OrderResult(**data)
    except httpx.ConnectError:
        return OrderResult(success=False, ticker=payload.get("ticker", ""), side="", price=0, count=0, error="Executor not running")
    except Exception as e:
        return OrderResult(success=False, ticker=payload.get("ticker", ""), side="", price=0, count=0, error=str(e))


def _build_keyboard(opp: Opportunity, opp_id: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if opp.type == OpportunityType.ENTRY:
        cost = opp.market.yes_ask * opp.suggested_count
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ Buy {opp.suggested_count}× @ ${opp.market.yes_ask:.2f}",
                callback_data=f"buy:{opp_id}",
            ),
            InlineKeyboardButton("❌ Skip", callback_data=f"skip:{opp_id}"),
        ]])

    elif opp.type == OpportunityType.EXIT:
        buttons = []
        if opp.sell_profit > 0:
            buttons.append(InlineKeyboardButton(
                f"💰 Sell (+${opp.sell_profit:.2f})",
                callback_data=f"sell:{opp_id}",
            ))
        if opp.lock_profit > 0:
            buttons.append(InlineKeyboardButton(
                f"🔒 Lock (+${opp.lock_profit:.2f})",
                callback_data=f"lock:{opp_id}",
            ))
        buttons.append(InlineKeyboardButton("⏳ Hold", callback_data=f"hold:{opp_id}"))
        return InlineKeyboardMarkup([buttons])

    else:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Execute", callback_data=f"buy:{opp_id}"),
            InlineKeyboardButton("❌ Skip", callback_data=f"skip:{opp_id}"),
        ]])


async def run_telegram_bot():
    from telegram import Update
    from telegram.ext import Application, CallbackQueryHandler, ContextTypes

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data = query.data
        action, opp_id = data.split(":", 1)

        _evict_stale()
        entry = _pending.pop(opp_id, None)

        if entry is None:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("⏰ Expired — opportunity is stale.")
            return

        opp, _ = entry
        await query.edit_message_reply_markup(reply_markup=None)

        if action == "skip":
            await query.message.reply_text("❌ Skipped.")
            return

        if action == "hold":
            await query.message.reply_text("⏳ Holding position.")
            return

        if action == "buy":
            result = await _call_executor("/order/buy", {
                "ticker": opp.market.ticker,
                "side": opp.suggested_side,
                "count": opp.suggested_count,
                "price": opp.market.yes_ask if opp.suggested_side == "yes" else opp.market.no_ask,
            })
            if result.success:
                if _positions_store:
                    _positions_store.add(Position(
                        ticker=result.ticker,
                        title=opp.market.title,
                        side=result.side,
                        entry_price=result.price,
                        count=result.count,
                        sport=opp.market.sport or "",
                    ))
                await query.message.reply_text(
                    f"✅ *Bought* {result.count}× {result.side.upper()} "
                    f"`{result.ticker}` at ${result.price:.2f}",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text(f"❌ Order failed: {result.error}")

        elif action == "sell":
            pos = opp.position
            price = opp.market.yes_ask if pos.side == "yes" else opp.market.no_ask
            result = await _call_executor("/order/sell", {
                "ticker": pos.ticker,
                "side": pos.side,
                "count": pos.count,
                "price": price,
            })
            if result.success:
                profit = (result.price - pos.entry_price) * result.count
                if _positions_store:
                    _positions_store.remove(pos.ticker)
                await query.message.reply_text(
                    f"💰 *Sold* {result.count}× {result.side.upper()} "
                    f"`{result.ticker}` at ${result.price:.2f}\n"
                    f"Profit: ${profit:.2f}",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text(f"❌ Sell failed: {result.error}")

        elif action == "lock":
            pos = opp.position
            opposite_side = "no" if pos.side == "yes" else "yes"
            opposite_price = opp.market.no_ask if pos.side == "yes" else opp.market.yes_ask
            result = await _call_executor("/order/lock", {
                "ticker": pos.ticker,
                "side": opposite_side,
                "count": pos.count,
                "price": opposite_price,
            })
            if result.success:
                locked = (1.0 - pos.entry_price - result.price) * pos.count
                if _positions_store:
                    _positions_store.remove(pos.ticker)
                await query.message.reply_text(
                    f"🔒 *Locked* — bought {result.count}× {result.side.upper()} "
                    f"`{result.ticker}` at ${result.price:.2f}\n"
                    f"Guaranteed profit: ${locked:.2f}",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text(f"❌ Lock failed: {result.error}")

    app.add_handler(CallbackQueryHandler(handle_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        while True:
            opp = await _queue.get()
            _evict_stale()

            opp_id = uuid.uuid4().hex[:8]
            _pending[opp_id] = (opp, time.time())

            keyboard = _build_keyboard(opp, opp_id)
            msg = _format_message(opp)

            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
