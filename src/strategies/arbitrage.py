"""Strategy 1 — Binary Arbitrage / Spread Scanner.

Scans the configured tickers/series for synthetic mispricings where
YES ask + NO ask <= 100c - min_profit threshold, then buys both legs at the
displayed asks. If both legs fill, the pair pays out 100c at settlement
regardless of outcome, locking in the spread (before fees — set
min_profit_cents high enough to clear Kalshi's fee schedule).
"""
from __future__ import annotations

from .base import OrderIntent, Strategy, StrategyContext, scan_for_arbitrage


class ArbitrageStrategy(Strategy):
    name = "arbitrage"
    label = "Binary Arbitrage Scanner"
    places_orders = True

    async def scan_once(self, ctx: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        opportunities = await scan_for_arbitrage(ctx, ctx.settings.min_profit_cents)
        for opp in opportunities:
            await ctx.log(
                f"ARB {opp['ticker']}: yes_ask={opp['yes_ask']}c + no_ask={opp['no_ask']}c "
                f"= {opp['combined']}c (edge {opp['edge_cents']}c)",
                "edge",
            )
            count = ctx.settings.contracts_per_side
            reason = f"arb edge {opp['edge_cents']}c"
            intents.append(
                OrderIntent(opp["ticker"], "yes", "buy", count, opp["yes_ask"], reason)
            )
            intents.append(
                OrderIntent(opp["ticker"], "no", "buy", count, opp["no_ask"], reason)
            )
        return intents
