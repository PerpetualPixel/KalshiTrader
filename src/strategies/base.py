"""Strategy interface. Each strategy implements `scan_once`; the engine owns
the loop, pacing, order submission, and risk checks."""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..config import BotSettings
from ..kalshi_client import KalshiClient

if TYPE_CHECKING:
    pass

MARKET_PAGE_LIMIT = 1000  # Kalshi's max page size for /markets
MAX_MARKET_PAGES = 4  # safety cap: at most 4000 markets per series


@dataclasses.dataclass
class OrderIntent:
    """What a strategy wants to trade; the engine applies risk + submits."""

    ticker: str
    side: str  # yes | no
    action: str  # buy | sell
    count: int
    price_cents: int
    reason: str = ""


@dataclasses.dataclass
class StrategyContext:
    client: KalshiClient
    settings: BotSettings
    log: Callable[[str, str], Awaitable[None]]  # (message, level)


class Strategy:
    name: str = "base"
    label: str = "Base"
    places_orders: bool = True

    async def scan_once(self, ctx: StrategyContext) -> list[OrderIntent]:
        raise NotImplementedError

    def on_order_result(self, intent: OrderIntent, ok: bool) -> None:
        """Called once per intent after the engine has tried to submit it.

        `ok` is False when risk blocked the order or Kalshi rejected it. A
        strategy that reserves internal state when it emits an intent needs
        this to release that reservation; the default is to ignore it."""


def market_prices(market: dict[str, Any]) -> dict[str, int | None]:
    """Best bid/ask straight from a /markets list entry (no orderbook call).

    Kalshi reports 0 on an empty book side; treat anything outside 1-99 as
    missing, and derive the NO side from YES when it isn't given directly
    (no_bid = 100 - yes_ask, no_ask = 100 - yes_bid).
    """

    def norm(value: Any) -> int | None:
        return value if isinstance(value, int) and 1 <= value <= 99 else None

    yes_bid = norm(market.get("yes_bid"))
    yes_ask = norm(market.get("yes_ask"))
    no_bid = norm(market.get("no_bid"))
    no_ask = norm(market.get("no_ask"))
    if no_bid is None and yes_ask is not None:
        no_bid = 100 - yes_ask
    if no_ask is None and yes_bid is not None:
        no_ask = 100 - yes_bid
    return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask}


async def gather_target_markets(
    ctx: StrategyContext, series_csv: str, tickers_csv: str
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Full market objects for explicit tickers plus every open market in the
    listed series, paginating so busy series (a Grand Slam's worth of tennis
    matches) aren't silently truncated at the first page.

    Returns (ticker -> market, series -> open market count).
    """
    markets: dict[str, dict[str, Any]] = {}
    per_series: dict[str, int] = {}

    explicit = [t.strip().upper() for t in tickers_csv.split(",") if t.strip()]
    if explicit:
        data = await ctx.client.get_markets(
            tickers=",".join(explicit), limit=max(len(explicit), 1)
        )
        for m in data.get("markets", []):
            if m.get("ticker"):
                markets[m["ticker"]] = m

    for series in [s.strip().upper() for s in series_csv.split(",") if s.strip()]:
        found = 0
        cursor: str | None = None
        for _ in range(MAX_MARKET_PAGES):
            data = await ctx.client.get_markets(
                status="open",
                series_ticker=series,
                limit=MARKET_PAGE_LIMIT,
                cursor=cursor,
            )
            page = data.get("markets", [])
            for m in page:
                if m.get("ticker") and m["ticker"] not in markets:
                    markets[m["ticker"]] = m
                    found += 1
            cursor = data.get("cursor")
            if not cursor or not page:
                break
        per_series[series] = found

    return markets, per_series


async def resolve_target_tickers(ctx: StrategyContext) -> list[str]:
    """Union of explicitly-listed tickers and open markets from listed series."""
    markets, _ = await gather_target_markets(
        ctx, ctx.settings.arb_series, ctx.settings.arb_tickers
    )
    return list(markets)


async def scan_for_arbitrage(
    ctx: StrategyContext, min_profit_cents: int
) -> list[dict[str, Any]]:
    """Find markets where buying YES and NO together locks in a profit:
    yes_ask + no_ask <= 100 - min_profit_cents. Prices come from the market
    list itself, so a scan is a handful of API calls however many markets
    are being watched."""
    opportunities: list[dict[str, Any]] = []
    markets, _ = await gather_target_markets(
        ctx, ctx.settings.arb_series, ctx.settings.arb_tickers
    )
    for ticker, market in markets.items():
        prices = market_prices(market)
        yes_ask, no_ask = prices["yes_ask"], prices["no_ask"]
        if yes_ask is None or no_ask is None:
            continue
        combined = yes_ask + no_ask
        if combined <= 100 - min_profit_cents:
            opportunities.append(
                {
                    "ticker": ticker,
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "combined": combined,
                    "edge_cents": 100 - combined,
                }
            )
    return opportunities
