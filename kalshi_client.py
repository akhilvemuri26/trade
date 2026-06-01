import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import re

import httpx

from models import Market
from strategy_config import MAX_HOURS_TO_RESOLUTION

logger = logging.getLogger(__name__)

BASE_URL = "https://api.elections.kalshi.com"

# Game-winner moneyline series (NBA / MLB / NHL only)
GAME_MONEYLINE_SERIES: dict[str, str] = {
    "nba": "KXNBAGAME",
    "mlb": "KXMLBGAME",
    "nhl": "KXNHLGAME",
}

SERIES_TO_SPORT: dict[str, str] = {v: k for k, v in GAME_MONEYLINE_SERIES.items()}

MVE_TICKER_PREFIXES = ("kxmv", "kxmve", "kxmvecrosscategory")

NON_MONEYLINE_PHRASES = (
    "wins by over",
    "wins by under",
    "wins by ",
    " points scored",
    " runs scored",
)

@dataclass
class SettlementInfo:
    ticker: str
    status: str
    result: str  # yes, no, void, scalar
    settlement_ts: Optional[float] = None
    resolution_source: str = ""


def _market_volume(data: dict) -> float:
    """Best available volume metric; prefer 24h when present."""
    candidates = (
        data.get("volume_24h_fp"),
        data.get("volume_fp"),
        data.get("volume"),
        data.get("open_interest_fp"),
        data.get("liquidity_dollars"),
    )
    best = 0.0
    for raw in candidates:
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > best:
            best = val
    return best


def _parse_close_timestamp(data: dict) -> Optional[float]:
    """When Kalshi expects the market to resolve (prefer game time, not series end)."""
    for field in (
        "expected_expiration_time",
        "close_time",
        "latest_expiration_time",
        "expiration_time",
    ):
        raw = data.get(field)
        if not raw:
            continue
        try:
            text = str(raw).replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def _within_resolution_window(close_ts: float, now: float) -> bool:
    """True if market resolves after now and within MAX_HOURS_TO_RESOLUTION."""
    if close_ts <= now:
        return False
    return (close_ts - now) <= MAX_HOURS_TO_RESOLUTION * 3600


def _sport_from_series(data: dict) -> Optional[str]:
    series = (data.get("series_ticker") or "").upper()
    if series in SERIES_TO_SPORT:
        return SERIES_TO_SPORT[series]
    ticker = (data.get("ticker") or "").upper()
    for prefix, sport in SERIES_TO_SPORT.items():
        if ticker.startswith(prefix):
            return sport
    return None


def is_moneyline_market(data: dict) -> bool:
    """True if market is a single-game winner moneyline (not parlay/spread/total/prop)."""
    ticker = (data.get("ticker") or "").lower()
    title = (data.get("title") or "")
    title_lower = title.lower()

    if any(ticker.startswith(p) for p in MVE_TICKER_PREFIXES):
        return False
    if "," in title:
        return False
    if any(p in title_lower for p in NON_MONEYLINE_PHRASES):
        return False
    if ":" in title and "+" in title:
        return False

    series = (data.get("series_ticker") or "").upper()
    if series in SERIES_TO_SPORT:
        return True
    for prefix in SERIES_TO_SPORT:
        if ticker.startswith(prefix.lower()) or ticker.startswith(prefix):
            return True
    return False


def parse_settlement(raw: dict) -> Optional[SettlementInfo]:
    """Return settlement info when market has a known result."""
    status = (raw.get("status") or "").lower()
    result = (raw.get("result") or "").lower()
    source = "result"
    if result not in ("yes", "no", "void"):
        winner = (raw.get("winning_outcome") or raw.get("winner") or "").lower()
        if winner in ("yes", "no", "void"):
            result = winner
            source = "winning_outcome"
    if result not in ("yes", "no", "void"):
        return None
    if status not in ("finalized", "determined", "settled"):
        return None
    settlement_ts = None
    for field in ("settlement_ts", "settlement_time"):
        raw_ts = raw.get(field)
        if raw_ts:
            try:
                settlement_ts = datetime.fromisoformat(
                    str(raw_ts).replace("Z", "+00:00"),
                ).timestamp()
            except (ValueError, TypeError):
                pass
            break
    return SettlementInfo(
        ticker=raw.get("ticker", ""),
        status=status,
        result=result,
        settlement_ts=settlement_ts,
        resolution_source=source,
    )


def market_side_labels(title: str, ticker: str) -> dict[str, str]:
    """
    Best-effort parsing of moneyline sides for display/audit.
    Returns yes/no labels and ticker suffix code.
    """
    title = (title or "").strip().replace("?", "")
    yes_label = "YES"
    no_label = "NO"
    lowered = title.lower()

    # "<Team A> vs <Team B> Winner?"
    if " vs " in lowered:
        parts = re.split(r"\s+vs\s+", title, flags=re.IGNORECASE, maxsplit=1)
        if len(parts) == 2:
            yes_label, no_label = parts[0].strip(), parts[1].replace(" Winner", "").strip()
    # "<Team A> at <Team B> Winner?"
    elif " at " in lowered:
        parts = re.split(r"\s+at\s+", title, flags=re.IGNORECASE, maxsplit=1)
        if len(parts) == 2:
            yes_label, no_label = parts[0].strip(), parts[1].replace(" Winner", "").strip()

    # Strip game prefixes like "Game 5:"
    if ":" in yes_label:
        yes_label = yes_label.split(":", 1)[-1].strip()
    if ":" in no_label:
        no_label = no_label.split(":", 1)[-1].strip()

    suffix = ""
    if "-" in ticker:
        suffix = ticker.rsplit("-", 1)[-1].strip().upper()

    return {
        "yes_label": yes_label or "YES",
        "no_label": no_label or "NO",
        "yes_ticker_code": suffix,
    }


def settlement_payout_dollars(
    side: str,
    count: int,
    entry_price: float,
    result: str,
) -> float:
    """Dollar payout credited to paper balance at settlement."""
    if result == "void":
        return entry_price * count
    if side == "yes":
        return float(count) if result == "yes" else 0.0
    return float(count) if result == "no" else 0.0


def _parse_market(data: dict, *, for_exit: bool = False) -> Optional[Market]:
    try:
        if not is_moneyline_market(data):
            return None
        status = (data.get("status") or "").lower()
        if status in ("finalized", "determined", "settled", "closed"):
            return None
        yes_ask = float(data.get("yes_ask_dollars") or 0)
        no_ask = float(data.get("no_ask_dollars") or 0)
        yes_bid = float(data.get("yes_bid_dollars") or 0)
        no_bid = float(data.get("no_bid_dollars") or 0)
        if yes_ask <= 0 or no_ask <= 0:
            return None
        if yes_ask >= 1.0 or no_ask >= 1.0:
            if not for_exit:
                return None
        sport = _sport_from_series(data)
        if sport is None:
            return None
        resolves_at = _parse_close_timestamp(data)
        now = time.time()
        if not for_exit:
            if resolves_at is None or not _within_resolution_window(resolves_at, now):
                return None
        volume = _market_volume(data)
        return Market(
            ticker=data["ticker"],
            title=data.get("title", ""),
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_bid=yes_bid,
            no_bid=no_bid,
            sport=sport,
            volume=volume,
            resolves_at=resolves_at,
            series_ticker=data.get("series_ticker"),
        )
    except (KeyError, TypeError, ValueError):
        return None


class KalshiClient:
    """
    Read-only market data client. No authentication, no trading permissions.
    Uses Kalshi's public market data endpoints only.
    Caches market list to avoid hammering the API.
    """

    CACHE_TTL = 30
    PAGE_DELAY = 0.25

    def __init__(self, sports: Optional[list[str]] = None):
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: list[Market] = []
        self._cache_ts: float = 0
        self._sports_filter = sports

    async def __aenter__(self):
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=15)
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def _get_with_backoff(self, path: str, **params) -> dict:
        for attempt in range(5):
            resp = await self._client.get(path, params=params)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning("Rate limited, backing off %ds", wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise httpx.HTTPStatusError(
            "Rate limited after 5 retries", request=resp.request, response=resp
        )

    async def get_market_raw(self, ticker: str) -> Optional[dict]:
        """Fetch raw market dict (any status) for settlement checks."""
        try:
            data = await self._get_with_backoff(f"/trade-api/v2/markets/{ticker}")
            return data.get("market", data)
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch raw market %s: %s", ticker, e)
            return None

    async def get_sports_markets(self, sports: Optional[list[str]] = None) -> list[Market]:
        """Fetch open game-winner moneyline markets for NBA/MLB/NHL series."""
        now = time.time()
        if self._cache and (now - self._cache_ts) < self.CACHE_TTL:
            return self._cache

        target_sports = sports or self._sports_filter or list(GAME_MONEYLINE_SERIES.keys())
        seen: set[str] = set()
        markets: list[Market] = []

        for sport in target_sports:
            series = GAME_MONEYLINE_SERIES.get(sport)
            if not series:
                continue
            cursor = None
            while True:
                params = {
                    "status": "open",
                    "limit": 200,
                    "series_ticker": series,
                }
                if cursor:
                    params["cursor"] = cursor
                data = await self._get_with_backoff("/trade-api/v2/markets", **params)
                batch = data.get("markets", [])
                for m in batch:
                    ticker = m.get("ticker", "")
                    if ticker in seen:
                        continue
                    parsed = _parse_market(m)
                    if parsed:
                        seen.add(ticker)
                        markets.append(parsed)
                cursor = data.get("cursor")
                if not cursor or not batch:
                    break
                await asyncio.sleep(self.PAGE_DELAY)

        markets.sort(key=lambda m: m.volume, reverse=True)
        self._cache = markets
        self._cache_ts = time.time()
        with_vol = sum(1 for m in markets if m.volume > 0)
        top_vol = markets[0].volume if markets else 0
        logger.info(
            "Refreshed moneyline cache: %d markets (%d with volume, top vol=%.0f)",
            len(markets), with_vol, top_vol,
        )
        return markets

    async def get_market(self, ticker: str, *, for_entry: bool = False) -> Optional[Market]:
        """Fetch one market. Skips 24h resolution filter unless for_entry=True."""
        raw = await self.get_market_raw(ticker)
        if not raw:
            return None
        return _parse_market(raw, for_exit=not for_entry)


def _parse_market_for_exit(data: dict) -> Optional[Market]:
    """Backward-compatible alias."""
    return _parse_market(data, for_exit=True)
