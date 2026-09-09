"""Thin, synchronous Kalshi Trade API v2 client.

Public market data needs no credentials. Portfolio and order endpoints need a
`RequestSigner`. Every call raises `KalshiError` on a non-2xx response so the
engine can decide whether to skip the cycle or halt.
"""
from __future__ import annotations

import email.utils
import logging
import random
import time
import uuid
from typing import Any

import httpx

from kalshitrader.kalshi.auth import RequestSigner, load_private_key
from kalshitrader.kalshi.models import Market, Orderbook, Position

log = logging.getLogger(__name__)


class KalshiError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"Kalshi API {status}: {message}")
        self.status = status
        self.payload = payload


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        private_key_pem: str | None = None,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self.signer: RequestSigner | None = None
        self._logged_drift_ms: int | None = None
        self._logged_drift_at: float = 0.0
        if api_key_id and (private_key_path or private_key_pem):
            self.signer = RequestSigner(api_key_id, load_private_key(private_key_path, private_key_pem))

    # ------------------------------------------------------------------ core
    @property
    def authenticated(self) -> bool:
        return self.signer is not None

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None, auth: bool = False) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if auth and not self.signer:
            raise KalshiError(401, "endpoint requires credentials but none are configured")
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        last_exc: Exception | None = None
        resynced = False
        for attempt in range(self.max_retries + 1):
            if auth and self.signer:  # always sign fresh: the timestamp must be current
                headers.update(self.signer.headers(method, url))
            try:
                resp = self._http.request(method, path, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                last_exc = exc
                self._backoff(attempt)
                continue
            self._note_server_clock(resp)
            if resp.status_code == 429:
                last_exc = KalshiError(429, resp.text[:300])
                self._backoff(attempt, retry_after=resp.headers.get("retry-after"))
                continue
            if resp.status_code >= 500:
                last_exc = KalshiError(resp.status_code, resp.text[:300])
                self._backoff(attempt)
                continue
            if resp.status_code == 401 and auth and self.signer and not resynced:
                # Most often clock drift rather than a bad key: resync and try once more.
                resynced = True
                drift = self.signer.drift_ms
                log.warning("Kalshi rejected a signature (401); retrying with clock drift %+d ms", drift)
                continue
            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = resp.text
                raise KalshiError(resp.status_code, str(payload)[:300], payload)
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()
        assert last_exc is not None
        raise last_exc if isinstance(last_exc, KalshiError) else KalshiError(0, str(last_exc))

    def _note_server_clock(self, resp: httpx.Response) -> None:
        """Track drift between our clock and Kalshi's, from the response Date header."""
        if self.signer is None:
            return
        date = resp.headers.get("date")
        if not date:
            return
        try:
            server_ms = int(email.utils.parsedate_to_datetime(date).timestamp() * 1000)
        except (TypeError, ValueError):
            return
        drift = server_ms - int(time.time() * 1000)
        # The Date header has one-second resolution and includes network latency,
        # so only correct drift large enough to threaten a signature.
        if abs(drift) > 5000:
            self.signer.sync_clock(server_ms)
            # The Date header is second-resolution and carries round-trip latency, so
            # successive readings of a steady offset scatter by several seconds. Logging
            # every material-looking change buries the run in identical warnings, so this
            # repeats at most every 10 minutes and only for a genuinely different offset.
            moved = self._logged_drift_ms is None or abs(self._logged_drift_ms - drift) > 15000
            stale = (time.time() - self._logged_drift_at) > 600
            if moved or stale:
                self._logged_drift_ms, self._logged_drift_at = drift, time.time()
                log.warning("local clock is %+.1fs from Kalshi's; signing with that offset. "
                            "Sync the system clock to remove this (Windows: `w32tm /resync` in an "
                            "admin prompt).", drift / 1000)
        elif self.signer.drift_ms and abs(drift) <= 5000:
            self.signer.drift_ms = 0
            self._logged_drift_ms, self._logged_drift_at = None, 0.0

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        """Exponential backoff with full jitter, honouring Retry-After when given."""
        if attempt >= self.max_retries:
            return
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
        ceiling = min(2**attempt * 0.5, 8.0)
        time.sleep(random.uniform(0, ceiling))

    # --------------------------------------------------------- market data
    def get_markets(
        self,
        *,
        status: str = "open",
        limit: int = 200,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        cursor: str | None = None,
        max_pages: int = 5,
    ) -> list[Market]:
        out: list[Market] = []
        page_size = min(limit, 1000)
        for _ in range(max_pages):
            data = self._request(
                "GET",
                "/markets",
                params={
                    "status": status,
                    "limit": page_size,
                    "series_ticker": series_ticker,
                    "event_ticker": event_ticker,
                    "cursor": cursor,
                },
            )
            out.extend(Market.from_api(m) for m in data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or len(out) >= limit:
                break
        return out[:limit]

    def get_market(self, ticker: str) -> Market:
        data = self._request("GET", f"/markets/{ticker}")
        return Market.from_api(data.get("market", data))

    def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        data = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return Orderbook.from_api(ticker, data)

    def get_event(self, event_ticker: str) -> dict:
        return self._request("GET", f"/events/{event_ticker}")

    # ------------------------------------------------------------ portfolio
    def get_balance(self) -> dict:
        """Returns {'balance': cents, 'portfolio_value': cents}."""
        return self._request("GET", "/portfolio/balance", auth=True)

    def get_positions(self, ticker: str | None = None) -> list[Position]:
        data = self._request(
            "GET", "/portfolio/positions", params={"ticker": ticker, "limit": 200, "settlement_status": "unsettled"}, auth=True
        )
        return [Position.from_api(p) for p in data.get("market_positions", []) if int(p.get("position") or 0) != 0]

    def get_orders(self, ticker: str | None = None, status: str | None = "resting") -> list[dict]:
        data = self._request("GET", "/portfolio/orders", params={"ticker": ticker, "status": status, "limit": 200}, auth=True)
        return data.get("orders", [])

    def get_fills(self, ticker: str | None = None, limit: int = 200) -> list[dict]:
        data = self._request("GET", "/portfolio/fills", params={"ticker": ticker, "limit": limit}, auth=True)
        return data.get("fills", [])

    def create_order(
        self,
        *,
        ticker: str,
        action: str,  # "buy" | "sell"
        side: str,  # "yes" | "no"
        count: int,
        price_cents: int,
        client_order_id: str | None = None,
        expiration_ts: int | None = None,
    ) -> dict:
        if action not in ("buy", "sell") or side not in ("yes", "no"):
            raise ValueError("action must be buy/sell and side must be yes/no")
        if not 1 <= price_cents <= 99:
            raise ValueError("price must be 1-99 cents")
        if count <= 0:
            raise ValueError("count must be positive")
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": "limit",
            "count": int(count),
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        body["yes_price" if side == "yes" else "no_price"] = int(price_cents)
        if expiration_ts is not None:
            body["expiration_ts"] = int(expiration_ts)
        data = self._request("POST", "/portfolio/orders", json=body, auth=True)
        return data.get("order", data)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}", auth=True)

    def get_order(self, order_id: str) -> dict:
        data = self._request("GET", f"/portfolio/orders/{order_id}", auth=True)
        return data.get("order", data)
