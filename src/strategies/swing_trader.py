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
    buy when drop >= swing_drop_cents, price is inside the configured price
    band, and the bid/ask spread is at most swing_max_spread_cents.

Every gate is a setting rather than a constant, because which one is binding
is not knowable in advance — it depends on the series being watched. When a
scan finds no dip, the strategy says which gate rejected how many sides and
how close the best candidate came, so the thresholds can be tuned from
evidence instead of guesswork.

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
    market_volume,
)

LOOKBACK_SECONDS = 300  # default window for "how far did it just fall"
FILL_GRACE_SECONDS = 30  # after order TTL + this, an unfilled entry is dropped

# Why a side was passed over, in the order the gates are applied. Reported as
# a per-scan tally so a strategy that never fires can be diagnosed from the
# activity log alone.
SKIP_LABELS = {
    "no_ask": "no ask quoted",
    "held": "already in this position",
    "no_slots": "position limit reached",
    "band": "price outside band",
    "no_bid": "no bid quoted",
    "spread": "spread too wide",
    "drop": "dip too small",
}


def swing_drop(
    history: list[tuple[float, int]],
    now: float,
    current_ask: int,
    lookback_seconds: float = LOOKBACK_SECONDS,
) -> int:
    """How far the ask has fallen from its recent high inside the lookback."""
    past = [ask for ts, ask in history if now - ts <= lookback_seconds]
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
        # (ticker, side) -> {"price", "count", "ts", "confirmed"} for entries
        # we ordered. An entry is provisional until the engine reports the
        # order was accepted; see `on_order_result`.
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_coverage: tuple | None = None
        self._last_scan: dict[str, Any] = {}

    async def _targets(self, ctx: StrategyContext) -> dict[str, dict[str, Any]]:
        if ctx.settings.swing_series.strip():
            markets, per_series = await gather_target_markets(
                ctx, ctx.settings.swing_series, ""
            )
        else:  # fall back to the arb scanner's targets
            markets, per_series = await gather_target_markets(
                ctx, ctx.settings.arb_series, ctx.settings.arb_tickers
            )
        markets, skipped = self._drop_inert(markets, ctx.settings.swing_min_volume)
        await self._report_coverage(ctx, per_series, len(markets), skipped)
        return markets

    @staticmethod
    def _drop_inert(
        markets: dict[str, dict[str, Any]], min_volume: int
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Drop markets that have barely traded.

        Kalshi lists a match starting tomorrow as an open market, and this
        strategy hunts in-game momentum, so watching those is pure noise: an
        untraded book does not move and can never show a dip. Markets whose
        volume Kalshi didn't report are kept — unknown is not zero.
        """
        if min_volume <= 0:
            return markets, 0
        kept = {}
        for ticker, market in markets.items():
            volume = market_volume(market)
            if volume is None or volume >= min_volume:
                kept[ticker] = market
        return kept, len(markets) - len(kept)

    async def _report_coverage(
        self,
        ctx: StrategyContext,
        per_series: dict[str, int],
        total: int,
        skipped: int = 0,
    ) -> None:
        """Log what the scan actually watches — but only when the picture
        changes, so the activity stream isn't spammed every cycle. A series
        that resolves to zero open markets is either between games or a
        ticker that doesn't exist on Kalshi; call those out explicitly."""
        snapshot = (total, skipped, tuple(sorted(per_series.items())))
        if snapshot == self._last_coverage:
            return
        self._last_coverage = snapshot
        empty = sorted(s for s, n in per_series.items() if n == 0)
        active = {s: n for s, n in per_series.items() if n > 0}
        summary = ", ".join(f"{s}:{n}" for s, n in sorted(active.items())) or "none"
        quiet = f" (skipped {skipped} below the volume floor)" if skipped else ""
        await ctx.log(
            f"watching {total} open market(s){quiet} — {summary}",
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

    def _note_ask(
        self, key: tuple[str, str], now: float, ask: int, lookback: float
    ) -> None:
        hist = self.ask_history.setdefault(key, deque())
        hist.append((now, ask))
        while hist and now - hist[0][0] > lookback * 2:
            hist.popleft()

    def _moving_sides(self, now: float, lookback: float) -> int:
        """Tracked sides whose ask changed at all inside the lookback.

        The distinction that matters when nothing trades: a scan watching 400
        sides of which 0 moved is not a threshold problem, it is watching
        markets where no game is being played.
        """
        moving = 0
        for hist in self.ask_history.values():
            asks = {ask for ts, ask in hist if now - ts <= lookback}
            if len(asks) > 1:
                moving += 1
        return moving

    def on_order_result(self, intent: OrderIntent, ok: bool) -> None:
        """The engine's verdict on an intent this strategy returned.

        An entry slot is reserved when the dip is spotted, but a rejected or
        risk-blocked order must not hold that slot: without this the strategy
        would count phantom positions against `swing_max_positions` and stop
        entering entirely after a few rejections.
        """
        if intent.action != "buy":
            return
        key = (intent.ticker, intent.side)
        entry = self.entries.get(key)
        if entry is None:
            return
        if ok:
            entry["confirmed"] = True
        else:
            del self.entries[key]

    def debug_state(self) -> dict[str, Any]:
        """State behind /api/debug/swing — what is tracked, held, and why the
        last scan did or didn't fire."""
        now = time.time()
        return {
            "tracking": {
                f"{t}:{s}": {
                    "samples": len(hist),
                    "current_ask": hist[-1][1] if hist else None,
                    "window_high": max((a for _ts, a in hist), default=None),
                    "age_seconds": round(now - hist[0][0], 1) if hist else 0.0,
                }
                for (t, s), hist in sorted(self.ask_history.items())
            },
            "entries": {
                f"{t}:{s}": {**entry, "age_seconds": round(now - entry["ts"], 1)}
                for (t, s), entry in sorted(self.entries.items())
            },
            "last_scan": self._last_scan,
        }

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
        lookback = float(settings.swing_lookback_seconds)
        band = (settings.swing_price_band_low, settings.swing_price_band_high)

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
        skips: dict[str, int] = {}
        best: tuple[int, str] | None = None  # (drop, "TICKER side @ask")
        sides_seen = 0

        def skip(reason: str) -> None:
            skips[reason] = skips.get(reason, 0) + 1

        for ticker, market in markets.items():
            prices = market_prices(market)
            for side in ("yes", "no"):
                ask = prices[f"{side}_ask"]
                bid = prices[f"{side}_bid"]
                if ask is None:
                    skip("no_ask")
                    continue
                sides_seen += 1
                key = (ticker, side)
                drop = swing_drop(
                    list(self.ask_history.get(key, ())), now, ask, lookback
                )
                self._note_ask(key, now, ask, lookback)
                if key in self.entries:
                    skip("held")
                    continue
                if open_slots <= 0:
                    skip("no_slots")
                    continue
                if not band[0] <= ask <= band[1]:
                    skip("band")
                    continue
                if bid is None:
                    skip("no_bid")
                    continue
                if ask - bid > settings.swing_max_spread_cents:
                    skip("spread")
                    continue
                # Past every liquidity gate: this side is a genuine candidate,
                # so its drop is the number worth reporting when nothing fires.
                if best is None or drop > best[0]:
                    best = (drop, f"{ticker} {side} @{ask}c")
                if drop < settings.swing_drop_cents:
                    skip("drop")
                    continue
                count = settings.contracts_per_side
                await ctx.log(
                    f"SWING DIP {ticker} {side}: ask fell {drop}c in "
                    f"{int(lookback) // 60}min to {ask}c — buying {count}",
                    "edge",
                )
                intents.append(
                    OrderIntent(ticker, side, "buy", count, ask, f"swing dip -{drop}c")
                )
                self.entries[key] = {
                    "price": ask, "count": count, "ts": now, "confirmed": False
                }
                open_slots -= 1

        moving = self._moving_sides(now, lookback)
        self._last_scan = {
            "at": now,
            "markets": len(markets),
            "sides_priced": sides_seen,
            "sides_moving": moving,
            "skips": dict(skips),
            "best_candidate_drop_cents": best[0] if best else None,
            "best_candidate": best[1] if best else None,
            "intents": len(intents),
        }
        await self._report_scan(ctx, settings, skips, best, sides_seen, moving)
        return intents

    async def _report_scan(
        self,
        ctx: StrategyContext,
        settings: Any,
        skips: dict[str, int],
        best: tuple[int, str] | None,
        sides_seen: int,
        moving: int,
    ) -> None:
        """One line naming the binding gate, logged only while the strategy is
        idle — enough to tell 'no dips yet' apart from 'every book is too wide
        to ever qualify', without a message per market per scan."""
        if not skips or not sides_seen:
            return
        tally = ", ".join(
            f"{SKIP_LABELS.get(r, r)}: {n}"
            for r, n in sorted(skips.items(), key=lambda kv: -kv[1])
        )
        if moving == 0:
            closest = (
                "no ask moved at all in the window — these books are idle, "
                "which is what an open market for a match that hasn't started "
                "looks like; raise swing_min_volume to skip them"
            )
        elif best is not None:
            closest = (
                f"best candidate {best[1]} fell {best[0]}c "
                f"(need {settings.swing_drop_cents}c)"
            )
        else:
            closest = (
                "no side cleared the price band and spread gates — widen "
                f"swing_max_spread_cents (now {settings.swing_max_spread_cents}c) "
                f"or the price band (now {settings.swing_price_band_low}-"
                f"{settings.swing_price_band_high}c)"
            )
        await ctx.log(
            f"no entry from {sides_seen} side(s), {moving} moving — {tally}; {closest}",
            "scan",
        )
