"""Risk engine. Every rule that can turn a trade into a PASS lives here.

The order of checks matters: cheap structural rejections first (halted,
price band, spread), then edge, then sizing. `RiskDecision.reasons` keeps the
full audit trail so the dashboard can show *why* the bot passed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshitrader.analysis.estimators import Estimate
from kalshitrader.analysis.ev import (
    contracts_for_budget,
    expected_value_cents,
    kelly_fraction,
    net_expected_value_cents,
)
from kalshitrader.analysis.signal import WIDE_SPREAD_FLAG, Signal, SignalAction
from kalshitrader.config import Settings
from kalshitrader.kalshi.models import Market, Orderbook


@dataclass
class PortfolioState:
    cash_dollars: float
    exposure_dollars: float
    open_positions: int
    daily_pnl_dollars: float
    consecutive_losses: int
    open_tickers: set[str] = field(default_factory=set)
    halted: bool = False


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    size: int = 0


class RiskManager:
    def __init__(self, settings: Settings):
        self.s = settings

    # ------------------------------------------------------- circuit breakers
    def circuit_breaker(self, state: PortfolioState) -> str | None:
        if state.halted:
            return "bot halted"
        if state.daily_pnl_dollars <= -abs(self.s.daily_loss_limit_dollars):
            return f"daily loss limit hit ({state.daily_pnl_dollars:+.2f} USD)"
        if state.consecutive_losses >= self.s.max_consecutive_losses:
            return f"{state.consecutive_losses} consecutive losses"
        return None

    # --------------------------------------------------------------- filters
    def market_eligible(self, market: Market, now: datetime | None = None) -> str | None:
        if not market.is_tradeable:
            return f"status={market.status}"
        hours = market.hours_to_close(now or datetime.now(timezone.utc))
        if hours is not None:
            if hours < self.s.min_hours_to_close:
                return f"closes in {hours:.1f}h (< {self.s.min_hours_to_close}h)"
            if hours > self.s.max_hours_to_close:
                return f"closes in {hours / 24:.0f}d (> {self.s.max_hours_to_close / 24:.0f}d)"
        if market.volume_24h < self.s.min_volume_24h:
            return f"24h volume {market.volume_24h} < {self.s.min_volume_24h}"
        if market.yes_ask <= 0 or market.no_ask <= 0:
            return "no ask on one side"
        return None

    # ---------------------------------------------------------------- scoring
    def build_signal(
        self,
        market: Market,
        book: Orderbook | None,
        est: Estimate | None,
        state: PortfolioState,
        now: datetime | None = None,
    ) -> Signal:
        """Turn a market + estimate into a fully-specified Signal (trade or PASS)."""
        s = self.s
        reasons: list[str] = []
        flags: list[str] = []
        p_mkt = market.yes_mid / 100.0

        base = dict(
            ticker=market.ticker,
            title=market.title,
            p_market=p_mkt,
            close_time=market.close_time,
        )

        def passing(reason: str, **kw) -> Signal:
            reasons.append(reason)
            return Signal(
                action=SignalAction.PASS,
                entry_price=0,
                p_true=kw.get("p_true", p_mkt),
                confidence=kw.get("confidence", 0.0),
                ev_gross=kw.get("ev_gross", 0.0),
                ev_net=kw.get("ev_net", 0.0),
                spread=kw.get("spread", market.yes_spread),
                depth=kw.get("depth", 0),
                rationale="; ".join(reasons),
                flags=flags,
                estimator=kw.get("estimator", ""),
                **base,
            )

        breaker = self.circuit_breaker(state)
        if breaker:
            return passing(breaker)
        ineligible = self.market_eligible(market, now)
        if ineligible:
            return passing(ineligible)
        if est is None:
            return passing("no estimate available")
        if market.ticker in state.open_tickers:
            return passing("already holding this market", p_true=est.p_yes, confidence=est.confidence, estimator=est.source)

        # Pick the side with the better net EV.
        yes_ask = book.best_yes_ask if book and book.best_yes_ask else market.yes_ask
        no_ask = book.best_no_ask if book and book.best_no_ask else market.no_ask
        ev_yes = net_expected_value_cents(est.p_yes, yes_ask, s.fee_rate)
        ev_no = net_expected_value_cents(1 - est.p_yes, no_ask, s.fee_rate)
        if ev_yes >= ev_no:
            action, entry, p_side = SignalAction.BUY_YES, yes_ask, est.p_yes
            spread = (yes_ask - (book.best_yes_bid or market.yes_bid)) if book else market.yes_spread
            depth = book.depth_at_yes_ask() if book else 0
        else:
            action, entry, p_side = SignalAction.BUY_NO, no_ask, 1 - est.p_yes
            spread = (no_ask - (book.best_no_bid or market.no_bid)) if book else market.no_spread
            depth = book.depth_at_no_ask() if book else 0
        ev_gross = expected_value_cents(p_side, entry)
        ev_net = net_expected_value_cents(p_side, entry, s.fee_rate)
        common = dict(p_true=est.p_yes, confidence=est.confidence, ev_gross=ev_gross, ev_net=ev_net, spread=spread, depth=depth, estimator=est.source)

        if spread > s.max_spread_cents:
            flags.append(WIDE_SPREAD_FLAG)
            if s.reject_wide_spread:
                return passing(f"spread {spread}c > {s.max_spread_cents}c", **common)
        if not s.min_price_cents <= entry <= s.max_price_cents:
            return passing(f"entry {entry}c outside [{s.min_price_cents}, {s.max_price_cents}]c band", **common)
        if est.confidence < s.min_confidence:
            return passing(f"confidence {est.confidence:.2f} < {s.min_confidence:.2f}", **common)
        if ev_net < s.min_edge_cents:
            return passing(f"net edge {ev_net:+.1f}c < {s.min_edge_cents:.1f}c", **common)
        if book is not None and depth < s.min_depth_contracts:
            return passing(f"depth {depth} < {s.min_depth_contracts} contracts at ask", **common)
        if state.open_positions >= s.max_open_positions:
            return passing(f"{state.open_positions} open positions (max {s.max_open_positions})", **common)

        size = self.size_position(p_side, entry, state)
        if size <= 0:
            return passing("no room under exposure limits", **common)
        if book is not None:
            size = min(size, depth)

        tp = min(99, entry + s.take_profit_cents)
        sl = max(1, entry - s.stop_loss_cents)
        rationale = f"{est.source}: {est.rationale}" if est.rationale else est.source
        return Signal(
            action=action,
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            size=size,
            rationale=rationale[:400],
            flags=flags,
            **common,
            **base,
        )

    # ----------------------------------------------------------------- sizing
    def size_position(self, p_side: float, entry: int, state: PortfolioState) -> int:
        s = self.s
        f = kelly_fraction(p_side, entry) * s.kelly_fraction
        bankroll = max(0.0, state.cash_dollars)
        kelly_budget = bankroll * f
        room = max(0.0, s.max_total_exposure_dollars - state.exposure_dollars)
        budget = min(kelly_budget, s.max_position_dollars, room, bankroll)
        if budget < s.min_position_dollars:
            # Better no position than one too small to be worth the fees. Also stops the
            # bot dribbling the last dollar of exposure into a token trade.
            return 0
        return min(contracts_for_budget(budget, entry), s.max_contracts_per_order)

    # ------------------------------------------------------------------ exits
    def exit_reason(self, side: str, entry: int, tp: int, sl: int, current_bid: int | None, p_true: float | None) -> str | None:
        """Decide whether an open position should be closed at `current_bid`."""
        if current_bid is None or current_bid <= 0:
            return None
        if sl and current_bid <= sl:
            return "stop-loss"
        if current_bid >= tp:
            if self.s.hold_to_settlement_if_edge and p_true is not None:
                p_side = p_true if side == "yes" else 1 - p_true
                if net_expected_value_cents(p_side, current_bid, self.s.fee_rate) >= self.s.min_edge_cents:
                    return None  # still positive EV vs. current bid: let it ride
            return "take-profit"
        return None
