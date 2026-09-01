"""Strategy 4 — Live Swing Trader.

Watches live markets for sharp price drops (an overreaction to in-game
momentum — a lost set, a home run) and buys the dip, then exits on a
take-profit, stop-loss, or max-hold timer. Positions are cashed out, never
held to settlement, so this strategy carries directional risk: the stop-loss
is what bounds a swing that never comes back.

Entry (per ticker, per side):
    drop = max(ask over lookback window) - current ask
    buy when drop >= swing_drop_cents, price is inside PRICE_BAND, and the
    bid/ask spread is at most MAX_SPREAD_CENTS (liquidity guard).

Exit (positions this strategy entered, verified against the live positions
endpoint so unfilled orders age out on their own):
    sell at bid when bid >= entry + swing_take_profit_cents (take profit)
    sell at bid when bid <= entry - swing_stop_loss_cents  (stop loss)
    sell at bid after swing_max_hold_minutes                (time exit)
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

from ..kalshi_client import best_prices
from .base import OrderIntent, Strategy, StrategyContext, resolve_target_tickers

LOOKBACK_SECONDS = 300  # window for "how far did it just fall"
PRICE_BAND = (15, 85)  # don't buy near-certainties or lost causes
MAX_SPREAD_CENTS = 6  # skip illiquid books where the spread eats the swing
FILL_GRACE_SECONDS = 30  # after order TTL + this, an unfilled entry is dropped


def swing_drop(history: list[tuple[float, int]], now: float, current_ask: int) -> int:
    """How far the ask has fallen from its recent high inside the lookback."""
    past = [ask for ts, ask in history if now - ts <= LOOKBACK_SECONDS]
    if not past:
        return 0
    return max(past) - current_ask


def exit_reason(
    entry_price: int,
    bid: int,
    held_seconds: float,
    take_profit_cents: int,
    stop_loss_cents: int,
    max_hold_seconds: float,
) -> str | None:
    """Why (if at all) a held swing position should be sold right now."""
    if bid >= entry_price + take_profit_cents:
        return "take_profit"
    if bid <= entry_price - stop_loss_cents:
        return "stop_loss"
    if held_seconds >= max_hold_seconds:
        return "max_hold"
    return None


class SwingTraderStrategy(Strategy):
    name = "swing"
    label = "Live Swing Trader"
    places_orders = True

    def __init__(self) -> None:
        # (ticker, side) -> deque of (timestamp, ask)
        self.ask_history: dict[tuple[str, str], deque] = {}
        # (ticker, side) -> {"price", "count", "ts"} for entries we ordered
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}

    async def _targets(self, ctx: StrategyContext) -> list[str]:
        series = [
            s.strip().upper() for s in ctx.settings.swing_series.split(",") if s.strip()
        ]
        if not series:
            return await resolve_target_tickers(ctx)
        tickers: list[str] = []
        for s in series:
            data = await ctx.client.get_markets(status="open", series_ticker=s, limit=50)
            for market in data.get("markets", []):
                if market.get("ticker") and market["ticker"] not in tickers:
                    tickers.append(market["ticker"])
        return tickers

    @staticmethod
    def _held(positions: dict[str, int], ticker: str, side: str) -> int:
        signed = positions.get(ticker, 0)
        return max(signed, 0) if side == "yes" else max(-signed, 0)

    def _note_ask(self, key: tuple[str, str], now: float, ask: int) -> None:
        hist = self.ask_history.setdefault(key, deque())
        hist.append((now, ask))
        while hist and now - hist[0][0] > LOOKBACK_SECONDS * 2:
            hist.popleft()

    async def scan_once(self, ctx: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        now = time.time()
        settings = ctx.settings
        max_hold_seconds = settings.swing_max_hold_minutes * 60.0

        positions_raw = await ctx.client.get_positions()
        positions = {
            str(p.get("ticker", "")): int(p.get("position", 0) or 0)
            for p in positions_raw.get("market_positions", [])
        }

        # ── Exits: manage positions we entered ────────────────────────
        for key, entry in list(self.entries.items()):
            ticker, side = key
            held = self._held(positions, ticker, side)
            if held <= 0:
                # Not holding: either the entry never filled (TTL expired) or
                # our exit completed. Give fills a grace window, then forget.
                if now - entry["ts"] > settings.order_ttl_seconds + FILL_GRACE_SECONDS:
                    del self.entries[key]
                continue
            try:
                book = await ctx.client.get_market_orderbook(ticker)
            except Exception as exc:  # noqa: BLE001
                await ctx.log(f"orderbook fetch failed for {ticker}: {exc}", "warn")
                continue
            prices = best_prices(book)
            bid = prices["yes_bid"] if side == "yes" else prices["no_bid"]
            if bid is None:
                continue
            reason = exit_reason(
                entry["price"], bid, now - entry["ts"],
                settings.swing_take_profit_cents,
                settings.swing_stop_loss_cents,
                max_hold_seconds,
            )
            if reason:
                pnl = bid - entry["price"]
                await ctx.log(
                    f"SWING EXIT ({reason}) {ticker} {side}: entered {entry['price']}c, "
                    f"selling {min(held, entry['count'])} @ {bid}c ({pnl:+d}c/contract)",
                    "edge",
                )
                intents.append(
                    OrderIntent(
                        ticker, side, "sell", min(held, entry["count"]), bid,
                        f"swing {reason} ({pnl:+d}c)",
                    )
                )
        # ── Entries: hunt for fresh dips ──────────────────────────────
        open_slots = settings.swing_max_positions - len(self.entries)
        for ticker in await self._targets(ctx):
            try:
                book = await ctx.client.get_market_orderbook(ticker)
            except Exception as exc:  # noqa: BLE001
                await ctx.log(f"orderbook fetch failed for {ticker}: {exc}", "warn")
                continue
            prices = best_prices(book)
            for side in ("yes", "no"):
                ask = prices[f"{side}_ask"]
                bid = prices[f"{side}_bid"]
                if ask is None:
                    continue
                key = (ticker, side)
                drop = swing_drop(list(self.ask_history.get(key, ())), now, ask)
                self._note_ask(key, now, ask)
                if key in self.entries or open_slots <= 0:
                    continue
                if not PRICE_BAND[0] <= ask <= PRICE_BAND[1]:
                    continue
                if bid is None or ask - bid > MAX_SPREAD_CENTS:
                    continue
                if drop >= settings.swing_drop_cents:
                    count = settings.contracts_per_side
                    await ctx.log(
                        f"SWING DIP {ticker} {side}: ask fell {drop}c in "
                        f"{LOOKBACK_SECONDS // 60}min to {ask}c — buying {count}",
                        "edge",
                    )
                    intents.append(
                        OrderIntent(ticker, side, "buy", count, ask, f"swing dip -{drop}c")
                    )
                    self.entries[key] = {"price": ask, "count": count, "ts": now}
                    open_slots -= 1
        return intents
