"""Swing strategy for live tennis: buy a well-backed player on a dip, sell on the rebound.

Entry, for each player of each match:
  gate 1  liveness: the market has moved recently (the match is in progress)
  gate 2  research: form, comeback plausibility, pre-match probability and confidence
          all clear the configured floors (or "trade without research" is on)
  gate 3  dip: the player's ask is at least `dip_cents` under their rolling high
  gate 4  value: research fair value beats the ask by `min_edge_cents` after fees
  gate 5  liquidity, price band, exposure and circuit breakers (RiskManager)

Exit: sell target = min(entry + take_profit, fair value); stop = entry - stop_loss.
Both are enforced every cycle by the engine's exit manager.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kalshitrader.analysis.ev import expected_value_cents, kalshi_fee_cents
from kalshitrader.analysis.signal import WIDE_SPREAD_FLAG, Signal, SignalAction
from kalshitrader.config import Settings
from kalshitrader.kalshi.models import Orderbook
from kalshitrader.markets.contest import Contest, Leg
from kalshitrader.risk.rules import PortfolioState, RiskManager
from kalshitrader.tennis.analyst import MatchAssessment
from kalshitrader.tracking.db import Store

log = logging.getLogger(__name__)


@dataclass
class PriceContext:
    live: bool
    rolling_high: int  # highest ask for this leg over the swing window
    last_move_minutes: float | None
    samples: int
    rolling_low: int = 0  # lowest ask over the same window
    volume_delta: int = 0


class SwingStrategy:
    def __init__(self, settings: Settings, store: Store, risk: RiskManager):
        self.s = settings
        self.store = store
        self.risk = risk

    # --------------------------------------------------------- price context
    def price_context(self, leg: Leg, now: datetime | None = None) -> PriceContext:
        now = now or datetime.now(timezone.utc)
        rows = self.store.snapshots(leg.ticker, limit=400)
        swing_cutoff = (now - timedelta(minutes=self.s.swing_window_minutes)).isoformat()
        live_cutoff = (now - timedelta(minutes=self.s.live_window_minutes)).isoformat()
        ask_key = "yes_ask" if leg.side == "yes" else "no_ask"
        window = [r for r in rows if r["ts"] >= swing_cutoff]
        highs = [r[ask_key] for r in window if r[ask_key]]
        rolling_high = max(highs + [leg.ask]) if highs else leg.ask
        # History only: the current ask must be compared against where the price has
        # been, or a price sitting flat at the low would read as "still falling".
        lows = [r[ask_key] for r in window if r[ask_key]]
        rolling_low = min(lows) if lows else 0
        recent = [r for r in rows if r["ts"] >= live_cutoff]
        prices = [(r["yes_bid"], r["yes_ask"]) for r in recent] + [(leg.market.yes_bid, leg.market.yes_ask)]
        moved = 0
        last_move: float | None = None
        for i in range(1, len(prices)):
            delta = max(abs(prices[i][0] - prices[i - 1][0]), abs(prices[i][1] - prices[i - 1][1]))
            if delta >= self.s.live_min_move_cents:
                moved = max(moved, delta)
                ts = recent[i - 1]["ts"] if i - 1 < len(recent) else now.isoformat()
                try:
                    last_move = (now - datetime.fromisoformat(ts)).total_seconds() / 60
                except ValueError:
                    last_move = 0.0
        # Contracts actually traded inside the window, from the cumulative `volume`
        # counter (`volume_24h` is a rolling window and can fall).
        volumes = [r["volume"] for r in recent if r.get("volume") is not None]
        volume_delta = max(0, volumes[-1] - volumes[0]) if len(volumes) >= 2 else 0
        required = self.s.live_min_volume_delta
        drifted = moved >= self.s.live_min_move_cents
        started = leg.market.has_started(now)
        if required <= 0:
            # Volume gate disabled: quotes only, still guarded by the schedule.
            live = drifted and started
        elif len(volumes) >= 2:
            # We can see the tape. Trading is the definitive signal and outranks the
            # schedule: `occurrence_datetime` is the *planned* start, and tennis runs
            # late constantly (rain, a five-setter on the same court). A match that is
            # genuinely trading is live whatever the calendar says.
            live = volume_delta >= required
        else:
            # No tape history yet (first scans of this market): fall back to quote
            # movement, but only once play was scheduled to begin, so pre-match drift
            # does not read as live.
            live = drifted and started
        return PriceContext(live=live, volume_delta=volume_delta, rolling_high=rolling_high, rolling_low=rolling_low, last_move_minutes=last_move, samples=len(rows))

    def _cooldown_remaining(self, ticker: str, now: datetime) -> float | None:
        """Minutes still to wait before this market may be bought again, or None."""
        window = self.s.reentry_cooldown_minutes
        if window <= 0:
            return None
        last = self.store.last_exit_at(ticker)
        if not last:
            return None
        try:
            closed = datetime.fromisoformat(last)
        except ValueError:
            return None
        if closed.tzinfo is None:
            closed = closed.replace(tzinfo=timezone.utc)
        elapsed = (now - closed).total_seconds() / 60
        return (window - elapsed) if elapsed < window else None

    # ---------------------------------------------------------------- decide
    def match_live(self, match: Contest, now: datetime | None = None) -> bool:
        """A match is live when any of its legs has moved recently."""
        now = now or datetime.now(timezone.utc)
        return any(self.price_context(leg, now).live for leg in match.legs.values())

    def evaluate(self, match: Contest, assessment: MatchAssessment | None, books: dict[str, Orderbook | None],
                 state: PortfolioState, now: datetime | None = None) -> list[Signal]:
        now = now or datetime.now(timezone.utc)
        live = self.match_live(match, now)
        return [
            self.evaluate_leg(match, player, leg, assessment, books.get(leg.ticker), state, now, live)
            for player, leg in match.legs.items()
            if player != "Opponent"
        ]

    def evaluate_leg(self, match: Contest, player: str, leg: Leg, assessment: MatchAssessment | None,
                     book: Orderbook | None, state: PortfolioState, now: datetime, match_live: bool | None = None) -> Signal:
        s = self.s
        ctx = self.price_context(leg, now)
        if match_live:
            ctx.live = True
        title = f"{match.title} · {player}"
        flags: list[str] = []
        p_market = leg.market.yes_mid / 100 if leg.side == "yes" else 1 - leg.market.yes_mid / 100
        p_pre, pa = assessment.for_player(player) if assessment else (p_market, None)
        conf = assessment.confidence if assessment else 0.0
        est = "research" if assessment else ""

        def passing(reason: str, ev: float = 0.0, ev_net: float = 0.0) -> Signal:
            return Signal(ticker=leg.ticker, title=title, action=SignalAction.PASS, entry_price=0, p_true=p_pre, p_market=p_market,
                          confidence=conf, ev_gross=ev, ev_net=ev_net, spread=leg.spread, depth=0, rationale=reason, flags=flags,
                          close_time=match.close_time, estimator=est)

        breaker = self.risk.circuit_breaker(state)
        if breaker:
            return passing(breaker)
        if not s.bot_enabled:
            return passing("bot disabled")
        if leg.ticker in state.open_tickers:
            return passing("already holding")
        cooled = self._cooldown_remaining(leg.ticker, now)
        if cooled is not None:
            # One decline, one trade. Without this the bot ladders into a falling
            # player, because the rolling high it measures against does not decay.
            return passing(f"traded this player already; {cooled:.0f}m of the {s.reentry_cooldown_minutes}m wait left")
        if leg.ask <= 0 or leg.bid <= 0:
            return passing("no two-sided quote")

        # gate 1: liveness
        if s.live_only and not ctx.live:
            if ctx.samples < 2 and not leg.market.has_started(now):
                starts = leg.market.starts_at.strftime("%d %b %H:%MZ") if leg.market.starts_at else "?"
                return passing(f"no price history yet and play is scheduled for {starts}")
            return passing(f"not live (no trades in the last {s.live_window_minutes}m)")

        # gate 2: research
        if assessment is None or assessment.confidence < s.min_confidence:
            if not s.allow_without_research:
                if assessment is None:
                    return passing("no research")
                return passing(f"research confidence {assessment.confidence:.2f} < {s.min_confidence:.2f}")
            if s.is_live and not s.allow_price_only_live:
                # The price-only rule buys half-retracements with no idea whether the
                # player is injured or simply being dominated. Opt-in for real money.
                return passing("price-only entries are off for live trading (Settings -> Price-only entries with real money)")
            # Price-only rule: assume half of the dip retraces. Fair value = ask + dip/2.
            p_pre, pa, est = min(0.97, (leg.ask + max(0, ctx.rolling_high - leg.ask) / 2) / 100), None, "price"
        if pa is not None:
            if pa.form_score < s.min_form_score:
                return passing(f"form {pa.form_score:.1f} < {s.min_form_score:.1f}")
            if pa.comeback_score < s.min_comeback_score:
                return passing(f"comeback {pa.comeback_score:.1f} < {s.min_comeback_score:.1f}")
            if p_pre < s.min_pre_match_p_win:
                return passing(f"pre-match P(win) {p_pre:.2f} < {s.min_pre_match_p_win:.2f}")
            if pa.fitness_risk >= 8:
                return passing(f"fitness risk {pa.fitness_risk:.0f}/10")
            if ctx.rolling_high > s.max_pre_match_price_cents and leg.ask > s.max_pre_match_price_cents:
                return passing(f"favourite priced {leg.ask}c > {s.max_pre_match_price_cents}c")

        # gate 3: dip
        dip = ctx.rolling_high - leg.ask
        if dip < s.dip_cents:
            return passing(f"dip {dip}c < {s.dip_cents}c (high {ctx.rolling_high}c, ask {leg.ask}c)")

        if s.require_stabilized and ctx.rolling_low and leg.ask < ctx.rolling_low:
            # Still making new lows: the swing has not turned yet.
            return passing(f"still falling ({leg.ask}c is a new {s.swing_window_minutes}m low)", p_pre, conf, est=est)

        # gate 4: value vs research fair value
        fair = p_pre
        ev = expected_value_cents(fair, leg.ask)
        # This is a round trip, not a hold to settlement: Kalshi charges the fee going
        # in and again coming out. An edge that ignores the exit fee is not an edge.
        exit_guess = min(99, leg.ask + s.take_profit_cents)
        round_trip = kalshi_fee_cents(leg.ask, s.fee_rate) + kalshi_fee_cents(exit_guess, s.fee_rate)
        ev_net = ev - round_trip
        if ev_net < s.min_edge_cents:
            return passing(f"edge {ev_net:+.1f}c < {s.min_edge_cents:.1f}c at {leg.ask}c (fair {fair * 100:.0f}c)", ev, ev_net)

        # gate 5: liquidity, band, exposure
        spread = leg.spread
        depth = (book.depth_at_yes_ask() if leg.side == "yes" else book.depth_at_no_ask()) if book else 0
        if depth <= 0:
            # The order-book endpoint can come back empty even when the market itself
            # reports resting size at the ask. Fall back to that rather than blocking.
            depth = leg.market.ask_size(leg.side)
        if spread > s.max_spread_cents:
            flags.append(WIDE_SPREAD_FLAG)
            if s.reject_wide_spread:
                return passing(f"spread {spread}c > {s.max_spread_cents}c", ev, ev_net)
        if not s.min_price_cents <= leg.ask <= s.max_price_cents:
            return passing(f"ask {leg.ask}c outside band", ev, ev_net)
        if book is not None and depth < s.min_depth_contracts:
            return passing(f"depth {depth} < {s.min_depth_contracts}", ev, ev_net)
        if state.open_positions >= s.max_open_positions:
            return passing(f"{state.open_positions} open positions (max {s.max_open_positions})", ev, ev_net)
        size = self.risk.size_position(fair, leg.ask, state)
        if book is not None:
            size = min(size, depth)
        if size <= 0:
            return passing("no room under exposure limits", ev, ev_net)

        # A target that does not clear the round trip is a loss dressed as a win:
        # Kalshi charges the fee on the way in and again on the way out.
        # Never wait for more than the position is worth, and never settle for a
        # target that the two fees would swallow.
        floor = leg.ask + int(round_trip) + 1
        tp = min(leg.ask + s.take_profit_cents, max(leg.ask + 1, int(round(fair * 100))))
        tp = min(99, max(tp, floor))
        # Reward and risk have to be compared AFTER fees, not before. Kalshi charges on
        # entry and on exit, so the fee comes out of the win and is added to the loss:
        # a 9c target against an 8c stop reads as 1.12:1 gross and is 0.49:1 net. The
        # gross comparison this used to make was flattering rather than protective.
        net_reward = (tp - leg.ask) - round_trip
        noise_floor = max(2, spread + 1)
        risk = s.stop_loss_cents
        if s.min_reward_risk > 0:
            # Widest stop whose net risk still clears the floor, never inside the
            # spread, where noise alone would fill it.
            allowed = net_reward / s.min_reward_risk - round_trip
            risk = min(risk, max(noise_floor, int(allowed)))
            net_risk = risk + kalshi_fee_cents(leg.ask, s.fee_rate) + kalshi_fee_cents(max(1, leg.ask - risk), s.fee_rate)
            if net_reward < s.min_reward_risk * net_risk:
                return passing(f"net reward {net_reward:.1f}c vs net risk {net_risk:.1f}c is worse than "
                               f"{s.min_reward_risk:.1f}:1 after fees", ev, ev_net)
        sl = max(1, leg.ask - risk) if s.use_stop_loss else 0
        # With no stop the whole stake is at risk, so that is what the ratio reports.
        net_risk = (risk + kalshi_fee_cents(leg.ask, s.fee_rate) + kalshi_fee_cents(sl, s.fee_rate)) if sl else float(leg.ask)
        why = (f"{player} dipped {dip}c from {ctx.rolling_high}c; risking {net_risk:.1f}c net to make "
               f"{net_reward:.1f}c net ({net_reward / net_risk if net_risk else 0:.2f}:1 after fees); ") + (
            f"research P(win)={p_pre:.2f}" if assessment is not None else f"price-only fair value {p_pre * 100:.0f}c (half the dip retraces)")
        if pa is not None and assessment is not None:
            why += f", form {pa.form_score:.0f}/10, comeback {pa.comeback_score:.0f}/10. {assessment.trade_view}"
        return Signal(
            ticker=leg.ticker, title=title, action=SignalAction.BUY_YES if leg.side == "yes" else SignalAction.BUY_NO,
            entry_price=leg.ask, p_true=p_pre, p_market=p_market, confidence=conf, ev_gross=ev, ev_net=ev_net,
            spread=spread, depth=depth, take_profit=tp, stop_loss=sl, size=size, rationale=why[:400], flags=flags,
            close_time=match.close_time, estimator=est,
        )
