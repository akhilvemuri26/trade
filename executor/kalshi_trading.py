import base64
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

BASE_URLS = {
    "demo": "https://demo-api.kalshi.co",
    "production": "https://api.elections.kalshi.com",
}


class KalshiTradingClient:
    """
    Authenticated Kalshi client using RSA-PSS key signing.
    No passwords, no 2FA — just a key pair.
    """

    def __init__(self, key_id: str, private_key_path: str = "", env: str = "demo", private_key_pem: str = ""):
        self._key_id = key_id
        if private_key_pem:
            self._private_key = self._load_key_from_string(private_key_pem)
        else:
            self._private_key = self._load_key_from_file(private_key_path)
        self._env = env
        self._base_url = BASE_URLS.get(env, BASE_URLS["demo"])
        self._client: Optional[httpx.AsyncClient] = None

    @staticmethod
    def _load_key_from_file(path: str):
        pem_data = Path(path).read_bytes()
        return serialization.load_pem_private_key(pem_data, password=None)

    @staticmethod
    def _load_key_from_string(pem_str: str):
        # Normalize escaped newlines from env vars (e.g. Fly.io secrets via shell substitution)
        pem_str = pem_str.replace("\\n", "\n")
        return serialization.load_pem_private_key(pem_str.encode(), password=None)

    def _sign(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode()

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    async def __aenter__(self):
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=15)
        # Verify connection with a lightweight call
        headers = self._sign("GET", "/trade-api/v2/portfolio/balance")
        resp = await self._client.get("/trade-api/v2/portfolio/balance", headers=headers)
        resp.raise_for_status()
        balance = resp.json()
        logger.info(
            "Connected to Kalshi %s API — balance: $%.2f",
            self._env,
            balance.get("balance", 0) / 100,
        )
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = self._sign(method, path)
        resp = await self._client.request(method, path, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price: float,
        action: str = "buy",
    ) -> dict:
        price_cents = int(round(price * 100))

        order_params = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": "limit",
            "count": count,
        }

        if side == "yes":
            order_params["yes_price"] = price_cents
        else:
            order_params["no_price"] = price_cents

        logger.info(
            "[%s] Placing order: %s %d× %s %s @ %d¢",
            self._env, action, count, side, ticker, price_cents,
        )

        result = await self._request(
            "POST",
            "/trade-api/v2/portfolio/orders",
            json=order_params,
        )

        order = result.get("order", result)
        logger.info("Order result: %s", order.get("status", "unknown"))
        return order

    async def get_positions(self) -> list[dict]:
        data = await self._request("GET", "/trade-api/v2/portfolio/positions")
        return data.get("market_positions", [])

    async def get_balance(self) -> float:
        data = await self._request("GET", "/trade-api/v2/portfolio/balance")
        return data.get("balance", 0) / 100
