"""Async Kalshi Trade API v2 client with RSA request signing.

Every request is authenticated with three headers:
    KALSHI-ACCESS-KEY:       the API key ID
    KALSHI-ACCESS-TIMESTAMP: unix time in milliseconds
    KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(timestamp + METHOD + path))

The signed path includes the /trade-api/v2 prefix but excludes the query
string, per Kalshi's API docs.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

logger = logging.getLogger(__name__)


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Kalshi API error {status_code}: {message}")


class KalshiClient:
    def __init__(self, api_base: str, key_id: str, private_key_path: str):
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id
        self._private_key = self._load_private_key(private_key_path)
        # Path prefix that must be part of the signed message.
        self._path_prefix = urlparse(self.api_base).path
        self._http = httpx.AsyncClient(base_url=self.api_base, timeout=15.0)
        # Simple client-side throttle: Kalshi's basic tier allows ~10 req/s.
        self._throttle = asyncio.Semaphore(5)

    @staticmethod
    def _load_private_key(path: str) -> RSAPrivateKey:
        pem = Path(path).expanduser().read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, RSAPrivateKey):
            raise ValueError(f"{path} is not an RSA private key")
        return key

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Build auth headers. `path` is relative to the API base, no query."""
        timestamp_ms = str(int(time.time() * 1000))
        full_path = f"{self._path_prefix}{path}"
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp_ms, method, full_path),
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        async with self._throttle:
            for attempt in range(retries + 1):
                headers = self.auth_headers(method, path)
                try:
                    resp = await self._http.request(
                        method, path, params=params, json=json, headers=headers
                    )
                except httpx.TransportError as exc:
                    if attempt >= retries:
                        raise KalshiAPIError(0, f"network error: {exc}") from exc
                    await asyncio.sleep(2**attempt)
                    continue
                if resp.status_code == 429 and attempt < retries:
                    await asyncio.sleep(2**attempt)
                    continue
                if resp.status_code >= 400:
                    raise KalshiAPIError(resp.status_code, resp.text)
                return resp.json() if resp.content else {}
        raise KalshiAPIError(0, "retries exhausted")

    # ── Portfolio ─────────────────────────────────────────────────────

    async def get_balance(self) -> int:
        """Available balance in cents."""
        data = await self._request("GET", "/portfolio/balance")
        return int(data.get("balance", 0))

    async def get_positions(self, **params: Any) -> dict[str, Any]:
        return await self._request("GET", "/portfolio/positions", params=params)

    async def get_fills(self, **params: Any) -> list[dict[str, Any]]:
        data = await self._request("GET", "/portfolio/fills", params=params)
        return data.get("fills", [])

    async def get_orders(self, **params: Any) -> list[dict[str, Any]]:
        data = await self._request("GET", "/portfolio/orders", params=params)
        return data.get("orders", [])

    async def get_settlements(self, **params: Any) -> list[dict[str, Any]]:
        data = await self._request("GET", "/portfolio/settlements", params=params)
        return data.get("settlements", [])

    async def place_order(
        self,
        ticker: str,
        side: str,  # "yes" | "no"
        action: str,  # "buy" | "sell"
        count: int,
        price_cents: int,
        expiration_ts: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Place a limit order via Kalshi's V2 create-order endpoint.

        Callers still speak the natural yes/no + buy/sell + integer-cents
        language; this translates to V2's single YES-denominated book, where
        every order is a bid or an ask at a dollar price:
            buy YES @p  -> bid @ p          sell YES @p -> ask @ p
            buy NO  @p  -> ask @ (100-p)    sell NO @p  -> bid @ (100-p)
        (Buying NO at p is the same trade as selling YES at 100-p.)

        V2's time_in_force has no timed expiry (only GTC/IOC/FOK), so
        `expiration_ts` is ignored here — the engine enforces the order TTL
        by cancelling stale orders itself.
        """
        if side not in ("yes", "no"):
            raise ValueError(f"invalid side {side!r}")
        if action not in ("buy", "sell"):
            raise ValueError(f"invalid action {action!r}")
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price {price_cents} out of range 1-99")
        if side == "yes":
            book_side = "bid" if action == "buy" else "ask"
            yes_price_cents = price_cents
        else:
            book_side = "ask" if action == "buy" else "bid"
            yes_price_cents = 100 - price_cents
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": str(count),
            "price": f"{yes_price_cents / 100:.2f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }
        data = await self._request("POST", "/portfolio/events/orders", json=body)
        return data.get("order", data)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        try:
            return await self._request("DELETE", f"/portfolio/orders/{order_id}")
        except KalshiAPIError as exc:
            # If the v1 cancel route is retired like the v1 create route was,
            # fall back to the V2 events path.
            if exc.status_code == 410:
                return await self._request(
                    "DELETE", f"/portfolio/events/orders/{order_id}"
                )
            raise

    # ── Market data ───────────────────────────────────────────────────

    async def get_markets(
        self,
        status: str | None = None,
        series_ticker: str | None = None,
        tickers: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if tickers:
            params["tickers"] = tickers
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> dict[str, Any]:
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    async def get_market_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        return data.get("orderbook", data)

    async def close(self) -> None:
        await self._http.aclose()


def best_prices(orderbook: dict[str, Any]) -> dict[str, int | None]:
    """Derive best bid/ask for YES and NO from a Kalshi orderbook.

    The orderbook lists resting *bids*: `yes` levels are bids to buy YES,
    `no` levels are bids to buy NO. A resting NO bid at price p is what a
    YES buyer lifts at (100 - p), so:
        yes_ask = 100 - best_no_bid,  no_ask = 100 - best_yes_bid
    """
    yes_levels = orderbook.get("yes") or []
    no_levels = orderbook.get("no") or []
    yes_bid = max((lvl[0] for lvl in yes_levels), default=None)
    no_bid = max((lvl[0] for lvl in no_levels), default=None)
    return {
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "yes_ask": (100 - no_bid) if no_bid is not None else None,
        "no_ask": (100 - yes_bid) if yes_bid is not None else None,
    }
