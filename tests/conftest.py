"""Shared fixtures: an in-memory fake Kalshi exchange served through httpx.MockTransport."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kalshitrader.config import Settings
from kalshitrader.kalshi.client import KalshiClient
from kalshitrader.tracking.db import Store


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_market(ticker="KXTEST-1", yes_bid=48, yes_ask=50, volume_24h=500, hours_to_close=48, status="open", result="", title="Test market"):
    close = datetime.now(timezone.utc) + timedelta(hours=hours_to_close)
    return {
        "ticker": ticker, "title": title, "event_ticker": "KXTEST", "series_ticker": "KXTEST",
        "yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": 100 - yes_ask, "no_ask": 100 - yes_bid,
        "last_price": yes_ask, "volume": volume_24h * 4, "volume_24h": volume_24h, "open_interest": 1000,
        "liquidity": 50000, "status": status, "result": result, "close_time": iso(close),
        "expiration_time": iso(close + timedelta(hours=1)), "rules_primary": "Settles YES if the thing happens.",
    }


class FakeExchange:
    """Minimal Kalshi v2 stand-in. Prices are driven by tests through `set_price`."""

    def __init__(self):
        self.markets: dict[str, dict] = {}
        self.depth = 200
        self.orders: list[dict] = []
        self.balance_cents = 100_000
        self.requests: list[httpx.Request] = []
        # Tickers whose /markets rows report no quote (0c) while the order book is
        # populated - the shape Kalshi returns for some in-play markets.
        self.hidden_quotes: set[str] = set()
        self.fail_next: list[int] = []  # status codes to return before succeeding

    def add(self, m: dict) -> None:
        self.markets[m["ticker"]] = m

    def set_price(self, ticker: str, yes_bid: int, yes_ask: int, traded: int = 25) -> None:
        """Move a market. A price change implies trades, so the tape moves too."""
        m = self.markets[ticker]
        m.update(yes_bid=yes_bid, yes_ask=yes_ask, no_bid=100 - yes_ask, no_ask=100 - yes_bid, last_price=yes_ask)
        m["volume"] = int(m.get("volume") or 0) + traded

    def settle(self, ticker: str, result: str) -> None:
        self.markets[ticker].update(status="settled", result=result)

    def _listed(self, m: dict) -> dict:
        if m["ticker"] not in self.hidden_quotes:
            return m
        return {**m, "yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0}

    def orderbook(self, m: dict) -> dict:
        """Build a book from the market, whichever field shape it uses."""
        from kalshitrader.kalshi.models import Market

        parsed = Market.from_api(m)
        if self.depth <= 0 or not parsed.has_quote:
            return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}

        def levels(top: int) -> list[list[str]]:
            # Kalshi's live shape: decimal dollar strings, ascending by price.
            return [[f"{(top - 1) / 100:.4f}", f"{self.depth}.00"], [f"{top / 100:.4f}", f"{self.depth}.00"]]

        return {"orderbook_fp": {"yes_dollars": levels(parsed.yes_bid), "no_dollars": levels(parsed.no_bid)}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={"error": "boom"})
        path = request.url.path.replace("/trade-api/v2", "")
        parts = path.strip("/").split("/")
        if request.method == "GET" and path == "/markets":
            # Kalshi's `status=open` filter returns markets whose own status field
            # reads "active" while trading is live. Model that here.
            wanted = request.url.params.get("status") or "open"
            aliases = {"open", "active"} if wanted == "open" else {wanted}
            ms = [self._listed(m) for m in self.markets.values() if m["status"] in aliases]
            st = request.url.params.get("series_ticker")
            if st:
                ms = [m for m in ms if m["series_ticker"] == st]
            return httpx.Response(200, json={"markets": ms, "cursor": ""})
        if request.method == "GET" and len(parts) == 2 and parts[0] == "markets":
            m = self.markets.get(parts[1])
            return httpx.Response(404, json={"error": "not found"}) if m is None else httpx.Response(200, json={"market": self._listed(m)})
        if request.method == "GET" and len(parts) == 3 and parts[2] == "orderbook":
            return httpx.Response(200, json=self.orderbook(self.markets[parts[1]]))
        if path == "/portfolio/balance":
            self._require_auth(request)
            return httpx.Response(200, json={"balance": self.balance_cents, "portfolio_value": self.balance_cents})
        if path == "/portfolio/positions":
            self._require_auth(request)
            return httpx.Response(200, json={"market_positions": []})
        if path == "/portfolio/orders" and request.method == "POST":
            self._require_auth(request)
            body = json.loads(request.content)
            oid = f"ord-{len(self.orders) + 1}"
            order = {**body, "order_id": oid, "status": "executed", "fill_count": body["count"], "remaining_count": 0, "taker_fees": 0}
            self.orders.append(order)
            return httpx.Response(201, json={"order": order})
        if len(parts) == 3 and parts[1] == "orders":
            self._require_auth(request)
            for o in self.orders:
                if o["order_id"] == parts[2]:
                    if request.method == "DELETE":
                        o["status"] = "canceled"
                        return httpx.Response(200, json={"order": o})
                    return httpx.Response(200, json={"order": o})
            return httpx.Response(404, json={"error": "no such order"})
        return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})

    @staticmethod
    def _require_auth(request: httpx.Request) -> None:
        for h in ("KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE", "KALSHI-ACCESS-TIMESTAMP"):
            assert h in request.headers, f"missing {h}"


@pytest.fixture
def exchange() -> FakeExchange:
    ex = FakeExchange()
    ex.add(make_market())
    return ex


@pytest.fixture
def rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture
def client(exchange: FakeExchange, rsa_pem: str) -> KalshiClient:
    c = KalshiClient("https://fake.kalshi.test/trade-api/v2", api_key_id="key-123", private_key_pem=rsa_pem,
                     transport=httpx.MockTransport(exchange.handler), max_retries=2)
    c._backoff = lambda attempt: None  # no sleeping in tests
    return c


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        db_path=str(tmp_path / "t.db"), manual_estimates_path=str(tmp_path / "est.json"),
        estimators=["manual", "microstructure"], min_volume_24h=10, min_edge_cents=4.0, min_depth_contracts=5,
        paper_starting_cash=1000.0, paper_slippage_cents=0,
        bot_enabled=True, max_position_dollars=25.0,
    )
    s.validate()
    return s


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings.db_path)
