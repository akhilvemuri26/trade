import logging
import os
import sys

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

from kalshi_trading import KalshiTradingClient

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")
PORT = int(os.getenv("EXECUTOR_PORT", "8100"))

trading_client: KalshiTradingClient = None


def _order_result(success: bool, ticker: str, side: str, price: float, count: int, error: str = ""):
    return web.json_response({
        "success": success,
        "ticker": ticker,
        "side": side,
        "price": price,
        "count": count,
        "error": error,
    })


async def handle_buy(request: web.Request):
    body = await request.json()
    ticker = body.get("ticker", "")
    side = body.get("side", "yes")
    count = body.get("count", 0)
    price = body.get("price", 0)

    if not ticker or count <= 0 or price <= 0:
        return _order_result(False, ticker, side, price, count, "Invalid parameters")

    try:
        order = await trading_client.place_order(ticker, side, count, price, action="buy")
        fill_price = order.get("yes_price", order.get("no_price", 0)) / 100
        fill_count = order.get("count", count)
        return _order_result(True, ticker, side, fill_price or price, fill_count)
    except Exception as e:
        logger.error("Buy order failed: %s", e)
        return _order_result(False, ticker, side, price, count, str(e))


async def handle_sell(request: web.Request):
    body = await request.json()
    ticker = body.get("ticker", "")
    side = body.get("side", "yes")
    count = body.get("count", 0)
    price = body.get("price", 0)

    if not ticker or count <= 0 or price <= 0:
        return _order_result(False, ticker, side, price, count, "Invalid parameters")

    try:
        order = await trading_client.place_order(ticker, side, count, price, action="sell")
        fill_price = order.get("yes_price", order.get("no_price", 0)) / 100
        fill_count = order.get("count", count)
        return _order_result(True, ticker, side, fill_price or price, fill_count)
    except Exception as e:
        logger.error("Sell order failed: %s", e)
        return _order_result(False, ticker, side, price, count, str(e))


async def handle_lock(request: web.Request):
    body = await request.json()
    ticker = body.get("ticker", "")
    side = body.get("side", "")
    count = body.get("count", 0)
    price = body.get("price", 0)

    if not ticker or not side or count <= 0 or price <= 0:
        return _order_result(False, ticker, side, price, count, "Invalid parameters")

    try:
        order = await trading_client.place_order(ticker, side, count, price, action="buy")
        fill_price = order.get("yes_price", order.get("no_price", 0)) / 100
        fill_count = order.get("count", count)
        return _order_result(True, ticker, side, fill_price or price, fill_count)
    except Exception as e:
        logger.error("Lock order failed: %s", e)
        return _order_result(False, ticker, side, price, count, str(e))


async def handle_positions(request: web.Request):
    try:
        positions = await trading_client.get_positions()
        return web.json_response({"positions": positions})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_health(request: web.Request):
    balance = await trading_client.get_balance()
    return web.json_response({"status": "ok", "env": KALSHI_ENV, "balance": balance})


async def on_startup(app: web.Application):
    global trading_client
    trading_client = KalshiTradingClient(
        KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV,
        private_key_pem=KALSHI_PRIVATE_KEY_PEM,
    )
    await trading_client.__aenter__()


async def on_cleanup(app: web.Application):
    if trading_client:
        await trading_client.__aexit__(None, None, None)


def main():
    if not KALSHI_KEY_ID or (not KALSHI_PRIVATE_KEY_PATH and not KALSHI_PRIVATE_KEY_PEM):
        logger.error("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY_PEM) must be set")
        sys.exit(1)

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_post("/order/buy", handle_buy)
    app.router.add_post("/order/sell", handle_sell)
    app.router.add_post("/order/lock", handle_lock)
    app.router.add_get("/positions", handle_positions)
    app.router.add_get("/health", handle_health)

    logger.info("Starting executor on localhost:%d (env: %s)", PORT, KALSHI_ENV)
    web.run_app(app, host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
