"""Strategy 2 — Edge / Fair Value Trader.

Takes model-estimated YES probabilities (entered per-ticker in the dashboard
settings as cents, e.g. 62 = 62%) and bids when the market offers the
contract cheaper than fair value minus an edge buffer:

    buy YES when yes_ask <= fair - edge_buffer
    buy NO  when no_ask  <= (100 - fair) - edge_buffer

Orders are placed at the ask as limit orders, so they never pay more than
the displayed price and expire via the engine's order TTL.
"""
from __future__ import annotations

from ..kalshi_client import best_prices
from .base import OrderIntent, Strategy, StrategyContext


class FairValueStrategy(Strategy):
    name = "fair_value"
    label = "Fair Value / Edge Trader"
    places_orders = True

    async def scan_once(self, ctx: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        buffer = ctx.settings.edge_buffer_cents
        for ticker, fair in ctx.settings.fair_values.items():
            ticker = ticker.strip().upper()
            if not ticker or not 1 <= fair <= 99:
                continue
            try:
                book = await ctx.client.get_market_orderbook(ticker)
            except Exception as exc:  # noqa: BLE001
                await ctx.log(f"orderbook fetch failed for {ticker}: {exc}", "warn")
                continue
            prices = best_prices(book)
            count = ctx.settings.contracts_per_side

            yes_ask = prices["yes_ask"]
            if yes_ask is not None and yes_ask <= fair - buffer:
                await ctx.log(
                    f"EDGE {ticker}: yes_ask {yes_ask}c vs fair {fair}c "
                    f"(edge {fair - yes_ask}c)",
                    "edge",
                )
                intents.append(
                    OrderIntent(
                        ticker, "yes", "buy", count, yes_ask,
                        f"fair {fair}c, edge {fair - yes_ask}c",
                    )
                )

            no_fair = 100 - fair
            no_ask = prices["no_ask"]
            if no_ask is not None and no_ask <= no_fair - buffer:
                await ctx.log(
                    f"EDGE {ticker}: no_ask {no_ask}c vs fair {no_fair}c "
                    f"(edge {no_fair - no_ask}c)",
                    "edge",
                )
                intents.append(
                    OrderIntent(
                        ticker, "no", "buy", count, no_ask,
                        f"fair(no) {no_fair}c, edge {no_fair - no_ask}c",
                    )
                )
        return intents
