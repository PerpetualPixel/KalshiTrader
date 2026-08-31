"""Strategy 3 — Signal-Only Watcher.

Runs the same mispricing detection as the arbitrage scanner but never fires
real orders: it only emits notifications to the activity stream. Useful for
validating thresholds in live markets before committing capital, or for
running alongside a manual trading workflow.
"""
from __future__ import annotations

from .base import OrderIntent, Strategy, StrategyContext, scan_for_arbitrage


class SignalWatcherStrategy(Strategy):
    name = "signal_watcher"
    label = "Signal-Only Watcher"
    places_orders = False

    async def scan_once(self, ctx: StrategyContext) -> list[OrderIntent]:
        opportunities = await scan_for_arbitrage(ctx, ctx.settings.min_profit_cents)
        for opp in opportunities:
            await ctx.log(
                f"SIGNAL {opp['ticker']}: combined ask {opp['combined']}c, "
                f"edge {opp['edge_cents']}c — no order placed (watch-only)",
                "signal",
            )
        return []  # never returns order intents
