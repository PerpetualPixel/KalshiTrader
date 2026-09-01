"""Strategy 4 — Live Swing Trader.

Watches live markets for sharp price drops (an overreaction to in-game
momentum — a lost set, a home run) and buys the dip, then exits on a
take-profit, stop-loss, or max-hold timer. Positions are cashed out, never
held to settlement, so this strategy carries directional risk: the stop-loss
is what bounds a swing that never comes back.

Prices come from the paginated /markets list (top-of-book bid/ask is included
there), so watching hundreds of markets costs a handful of API calls per
scan instead of one orderbook call per market.

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
from .base import (
    OrderIntent,
    Strategy,
    StrategyContext,
    gather_target_markets,
    market_prices,
)

LOOKBACK_SECONDS = 300  # window for "how far did it just fall"
PRICE_BAND = (15, 85)  # don't buy near-certainties or lost causes
MAX_SPREAD_CENTS = 8  # skip illiquid books where the spread eats the swing
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
        self._last_coverage: tuple | None = None

    async def _targets(self, ctx: StrategyContext) -> dict[str, dict[str, Any]]:
        if ctx.settings.swing_series.strip():
            markets, per_series = await gather_target_markets(
                ctx, ctx.settings.swing_series, ""
            )
        else:  # fall back to the arb scanner's targets
            markets, per_series = await gather_target_markets(
                ctx, ctx.settings.arb_series, ctx.settings.arb_tickers
            )
        await self._report_coverage(ctx, per_series, len(markets))
        return markets

    async def _report_coverage(
        self, ctx: StrategyContext, per_series: dict[str, int], total: int
    ) -> None:
        """Log what the scan actually watches — but only when the picture
        changes, so the activity stream isn't spammed every cycle. A series
        that resolves to zero open markets is either between games or a
        ticker that doesn't exist on Kalshi; call those out explicitly."""
        snapshot = (total, tuple(sorted(per_series.items())))
        if snapshot == self._last_coverage:
            return
        self._last_coverage = snapshot
        empty = sorted(s for s, n in per_series.items() if n == 0)
        active = {s: n for s, n in per_series.items() if n > 0}
        summary = ", ".join(f"{s}:{n}" for s, n in sorted(active.items())) or "none"
        await ctx.log(
            f"watching {total} open market(s) — {summary}",
            "info" if total else "warn",
        )
        if empty:
            await ctx.log(
                f"no open markets in: {', '.join(empty)} "
                "(no games right now, or the series ticker doesn't exist)",
                "warn",
            )

    @staticmethod
    def _held(positions: dict[str, int], ticker: str, side: str) -> int:
        signed = positions.get(ticker, 0)
        return max(signed, 0) if side == "yes" else max(-signed, 0)

    def _note_ask(self, key: tuple[str, str], now: float, ask: int) -> None:
        hist = self.ask_history.setdefault(key, deque())
        hist.append((now, ask))
        while hist and now - hist[0][0] > LOOKBACK_SECONDS * 2:
            hist.popleft()

    async def _side_prices(
        self, ctx: StrategyContext, markets: dict[str, dict], ticker: str, side: str
    ) -> tuple[int | None, int | None]:
        """(bid, ask) for one side — from this scan's market list when the
        ticker is watched, falling back to an orderbook call when it isn't
        (e.g. a held position whose series was removed from settings)."""
        if ticker in markets:
            prices = market_prices(markets[ticker])
        else:
            try:
                prices = best_prices(await ctx.client.get_market_orderbook(ticker))
            except Exception as exc:  # noqa: BLE001
                await ctx.log(f"orderbook fetch failed for {ticker}: {exc}", "warn")
                return None, None
        return prices[f"{side}_bid"], prices[f"{side}_ask"]

    async def scan_once(self, ctx: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        now = time.time()
        settings = ctx.settings
        max_hold_seconds = settings.swing_max_hold_minutes * 60.0

        markets = await self._targets(ctx)
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
            bid, _ask = await self._side_prices(ctx, markets, ticker, side)
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
        for ticker, market in markets.items():
            prices = market_prices(market)
            for side in ("yes", "no"):
                ask = prices[f"{side}_ask"]
                bid = prices[f"{side}_bid"]
                if ask is None:
                    continue
                key = (ticker, side)
                drop = swing_drop(list(self.ask_history.get(key, ())), now, ask)
                self._note_ask(key, now, ask)
                if key in self.entries:
                    continue
                if open_slots <= 0:
                    continue
                if not PRICE_BAND[0] <= ask <= PRICE_BAND[1]:
                    await ctx.log(
                        f"SWING {ticker} {side}: ask {ask}c outside PRICE_BAND {PRICE_BAND}",
                        "info",
                    )
                    continue
                if bid is None:
                    await ctx.log(
                        f"SWING {ticker} {side}: no bid data",
                        "info",
                    )
                    continue
                if ask - bid > MAX_SPREAD_CENTS:
                    spread = ask - bid
                    await ctx.log(
                        f"SWING {ticker} {side}: spread {spread}c > MAX_SPREAD_CENTS {MAX_SPREAD_CENTS}",
                        "info",
                    )
                    continue
                if drop < settings.swing_drop_cents:
                    await ctx.log(
                        f"SWING {ticker} {side}: drop {drop}c < threshold {settings.swing_drop_cents}c",
                        "info",
                    )
                    continue
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
