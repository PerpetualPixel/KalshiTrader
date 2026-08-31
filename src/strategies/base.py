"""Strategy interface. Each strategy implements `scan_once`; the engine owns
the loop, pacing, order submission, and risk checks."""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..config import BotSettings
from ..kalshi_client import KalshiClient, best_prices

if TYPE_CHECKING:
    pass


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


async def resolve_target_tickers(ctx: StrategyContext) -> list[str]:
    """Union of explicitly-listed tickers and open markets from listed series."""
    tickers = [t.strip().upper() for t in ctx.settings.arb_tickers.split(",") if t.strip()]
    for series in [s.strip().upper() for s in ctx.settings.arb_series.split(",") if s.strip()]:
        data = await ctx.client.get_markets(status="open", series_ticker=series, limit=50)
        for market in data.get("markets", []):
            if market.get("ticker") and market["ticker"] not in tickers:
                tickers.append(market["ticker"])
    return tickers


async def scan_for_arbitrage(
    ctx: StrategyContext, min_profit_cents: int
) -> list[dict[str, Any]]:
    """Find markets where buying YES and NO together locks in a profit:
    yes_ask + no_ask <= 100 - min_profit_cents."""
    opportunities: list[dict[str, Any]] = []
    for ticker in await resolve_target_tickers(ctx):
        try:
            book = await ctx.client.get_market_orderbook(ticker)
        except Exception as exc:  # noqa: BLE001 — one bad ticker shouldn't kill the scan
            await ctx.log(f"orderbook fetch failed for {ticker}: {exc}", "warn")
            continue
        prices = best_prices(book)
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
