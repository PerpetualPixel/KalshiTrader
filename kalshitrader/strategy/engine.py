"""The trading loop.

One `cycle()`:
  1. manage exits on open positions (settlement, take-profit, stop-loss)
  2. cancel stale resting orders (live only)
  3. scan markets -> estimate P_true -> risk -> Signal -> (maybe) execute
  4. snapshot equity and record the run

Everything the cycle decides is written to the Store, which is what the
dashboard reads. The engine never talks to the dashboard directly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshitrader.analysis.estimators import CompositeEstimator, Estimate, build_estimator
from kalshitrader.analysis.signal import Signal, SignalAction
from kalshitrader.config import Settings
from kalshitrader.execution.base import Broker
from kalshitrader.execution.paper import PaperBroker
from kalshitrader.kalshi.client import KalshiClient, KalshiError
from kalshitrader.kalshi.models import Market, Orderbook
from kalshitrader.risk.rules import PortfolioState, RiskManager
from kalshitrader.tracking.db import Store
from kalshitrader.tracking.metrics import compute_metrics

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    markets_scanned: int = 0
    signals: list[Signal] = field(default_factory=list)
    trades_opened: int = 0
    trades_closed: int = 0
    duration_ms: int = 0
    error: str | None = None

    @property
    def trade_signals(self) -> list[Signal]:
        return [s for s in self.signals if s.is_trade]


class Engine:
    def __init__(
        self,
        settings: Settings,
        client: KalshiClient,
        store: Store,
        broker: Broker,
        estimator: CompositeEstimator | None = None,
        risk: RiskManager | None = None,
        orderbook_budget: int = 40,
    ):
        self.s = settings
        self.client = client
        self.store = store
        self.broker = broker
        self.estimator = estimator or build_estimator(
            settings.estimators, manual_path=settings.manual_estimates_path, model=settings.anthropic_model, effort=settings.claude_effort
        )
        self.risk = risk or RiskManager(settings)
        self.orderbook_budget = orderbook_budget  # max order-book fetches per cycle
        self.mode = broker.mode

    # ------------------------------------------------------------ state
    def portfolio_state(self, cash: float | None = None) -> PortfolioState:
        open_trades = self.store.open_trades(self.mode)
        exposure = sum(t["entry_price"] * t["count"] / 100.0 for t in open_trades)
        metrics = compute_metrics(self.store.all_trades(), [], None)
        if cash is None:
            try:
                cash = self.broker.cash_dollars()
            except KalshiError as exc:
                log.warning("could not read balance: %s", exc)
                cash = 0.0
        return PortfolioState(
            cash_dollars=cash,
            exposure_dollars=exposure,
            open_positions=len(open_trades),
            daily_pnl_dollars=metrics["daily_pnl"],
            consecutive_losses=metrics["consecutive_losses"],
            open_tickers={t["ticker"] for t in open_trades},
            halted=bool(self.store.get_state("halted", False)),
        )

    # ------------------------------------------------------------ markets
    def fetch_markets(self) -> list[Market]:
        markets: list[Market] = []
        try:
            if self.s.series_tickers:
                for st in self.s.series_tickers:
                    markets.extend(self.client.get_markets(limit=self.s.scan_limit, series_ticker=st))
            elif self.s.event_tickers:
                for et in self.s.event_tickers:
                    markets.extend(self.client.get_markets(limit=self.s.scan_limit, event_ticker=et))
            else:
                markets = self.client.get_markets(limit=self.s.scan_limit)
        except KalshiError as exc:
            log.error("market fetch failed: %s", exc)
            raise
        seen: set[str] = set()
        unique = []
        for m in markets:
            if m.ticker not in seen:
                seen.add(m.ticker)
                unique.append(m)
        return unique

    def _book(self, ticker: str) -> Orderbook | None:
        try:
            return self.client.get_orderbook(ticker)
        except KalshiError as exc:
            log.debug("orderbook fetch failed for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------- exits
    def manage_exits(self) -> int:
        closed = 0
        for t in self.store.open_trades(self.mode):
            try:
                market = self.client.get_market(t["ticker"])
            except KalshiError as exc:
                log.warning("could not refresh %s for exit check: %s", t["ticker"], exc)
                continue
            self.store.add_snapshot(_snapshot_dict(market))
            if market.is_settled():
                won = market.result == t["side"]
                if isinstance(self.broker, PaperBroker):
                    self.broker.settle(t["count"], won)
                self.store.close_trade(t["id"], exit_price=100 if won else 0, exit_reason=f"settled {market.result}", status="settled")
                closed += 1
                log.info("SETTLED %s %s x%d -> %s", t["ticker"], t["side"], t["count"], "WIN" if won else "LOSS")
                continue
            if not market.is_tradeable:
                continue
            bid = market.yes_bid if t["side"] == "yes" else market.no_bid
            if t.get("close_requested"):
                # Asked for from the dashboard. A manual cash-out overrides the target,
                # the stop and the hold-for-edge rule: the person watching decided.
                reason = "manual cash-out" if bid and bid > 0 else None
                if reason is None:
                    log.warning("cash-out of %s requested but there is no bid to sell into", t["ticker"])
            else:
                reason = self.risk.exit_reason(t["side"], t["entry_price"], t["take_profit"], t["stop_loss"], bid, t.get("p_true"))
            if not reason:
                continue
            fill = self.broker.sell(t["ticker"], t["side"], bid, t["count"])
            self.store.add_order(mode=self.mode, ticker=t["ticker"], action="sell", side=t["side"], price=bid, count=t["count"],
                                 filled=fill.count, status=fill.status, broker_order_id=fill.broker_order_id, trade_id=t["id"], note=reason)
            if fill.count <= 0:
                log.warning("exit order for %s not filled (%s)", t["ticker"], fill.status)
                continue
            if fill.count < t["count"]:
                # partial exit: shrink the open trade, book the closed slice as its own trade
                remaining = t["count"] - fill.count
                with self.store.tx() as conn:
                    conn.execute("UPDATE trades SET count=? WHERE id=?", (remaining, t["id"]))
                slice_id = self.store.open_trade(mode=self.mode, ticker=t["ticker"], title=t["title"], side=t["side"], entry_price=t["entry_price"],
                                                 count=fill.count, take_profit=t["take_profit"], stop_loss=t["stop_loss"], p_true=t.get("p_true"),
                                                 fees=0.0, signal_id=t.get("signal_id"), close_time=t.get("close_time"))
                self.store.close_trade(slice_id, exit_price=fill.price, exit_reason=reason + " (partial)", extra_fees=fill.fees_dollars)
            else:
                self.store.close_trade(t["id"], exit_price=fill.price, exit_reason=reason, extra_fees=fill.fees_dollars)
            closed += 1
            log.info("EXIT %s %s x%d @ %dc (%s)", t["ticker"], t["side"], fill.count, fill.price, reason)
        return closed

    # ------------------------------------------------------------- entries
    def evaluate(self, market: Market, book: Orderbook | None, state: PortfolioState, now: datetime | None = None) -> Signal:
        est: Estimate | None = None
        if self.risk.market_eligible(market, now) is None and self.risk.circuit_breaker(state) is None:
            est = self.estimator.estimate(market, book)
        return self.risk.build_signal(market, book, est, state, now)

    def execute(self, sig: Signal, signal_id: int | None = None) -> bool:
        assert sig.side is not None
        fill = self.broker.buy(sig.ticker, sig.side, sig.entry_price, sig.size)
        self.store.add_order(mode=self.mode, ticker=sig.ticker, action="buy", side=sig.side, price=sig.entry_price, count=sig.size,
                             filled=fill.count, status=fill.status, broker_order_id=fill.broker_order_id, note=sig.action.value)
        if fill.count <= 0:
            log.warning("entry for %s not filled (%s)", sig.ticker, fill.status)
            return False
        self.store.open_trade(
            mode=self.mode, ticker=sig.ticker, title=sig.title, side=sig.side, entry_price=fill.price, count=fill.count,
            take_profit=sig.take_profit, stop_loss=sig.stop_loss, p_true=sig.p_true, fees=fill.fees_dollars, signal_id=signal_id,
            close_time=sig.close_time.isoformat() if sig.close_time else None,
        )
        if signal_id is not None:
            self.store.mark_signal_executed(signal_id)
        log.info("ENTER %s %s x%d @ %dc (TP %dc / SL %dc)", sig.ticker, sig.side.upper(), fill.count, fill.price, sig.take_profit, sig.stop_loss)
        return True

    def scan(self, execute: bool = True, markets: list[Market] | None = None) -> CycleResult:
        result = CycleResult()
        now = datetime.now(timezone.utc)
        markets = markets if markets is not None else self.fetch_markets()
        result.markets_scanned = len(markets)
        state = self.portfolio_state()
        # cheapest filters first so we only spend order-book calls on candidates
        candidates = [m for m in markets if self.risk.market_eligible(m, now) is None]
        candidates.sort(key=lambda m: -m.volume_24h)
        books_left = self.orderbook_budget
        for m in candidates:
            self.store.add_snapshot(_snapshot_dict(m))
            book = None
            if books_left > 0:
                book = self._book(m.ticker)
                books_left -= 1
            sig = self.evaluate(m, book, state, now)
            result.signals.append(sig)
            if not sig.is_trade:
                if sig.action != SignalAction.PASS or sig.ev_net > 0:
                    self.store.add_signal(sig.to_record())
                continue
            signal_id = self.store.add_signal(sig.to_record())
            log.info("\n%s", sig.format())
            if not execute:
                continue
            if self.execute(sig, signal_id):
                result.trades_opened += 1
                state = self.portfolio_state()
        return result

    # --------------------------------------------------------------- cycle
    def snapshot_equity(self) -> dict:
        open_trades = self.store.open_trades(self.mode)
        cash = self.broker.cash_dollars()
        positions_value = 0.0
        cost = 0.0
        for t in open_trades:
            cost += t["entry_price"] * t["count"] / 100.0
            snap = self.store.snapshots(t["ticker"], limit=1)
            if snap:
                bid = snap[-1]["yes_bid"] if t["side"] == "yes" else snap[-1]["no_bid"]
                positions_value += (bid or t["entry_price"]) * t["count"] / 100.0
            else:
                positions_value += t["entry_price"] * t["count"] / 100.0
        realized = sum(float(t["pnl"] or 0) for t in self.store.closed_trades(mode=self.mode, limit=100000))
        self.store.add_equity(mode=self.mode, cash=cash, positions_value=positions_value, realized_pnl=realized,
                              unrealized_pnl=positions_value - cost, open_positions=len(open_trades))
        return {"cash": cash, "positions_value": positions_value, "equity": cash + positions_value}

    def cycle(self, execute: bool = True) -> CycleResult:
        t0 = time.monotonic()
        result = CycleResult()
        try:
            result.trades_closed = self.manage_exits()
            self.broker.cancel_stale(self.s.scan_interval_seconds * 2)
            scanned = self.scan(execute=execute)
            result.markets_scanned = scanned.markets_scanned
            result.signals = scanned.signals
            result.trades_opened = scanned.trades_opened
            self.snapshot_equity()
        except KalshiError as exc:
            result.error = str(exc)
            log.error("cycle failed: %s", exc)
        except Exception as exc:  # keep the loop alive; the run table records it
            result.error = f"{type(exc).__name__}: {exc}"
            log.exception("cycle crashed")
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        self.store.add_run(mode=self.mode, markets_scanned=result.markets_scanned, signals=len(result.trade_signals),
                           trades_opened=result.trades_opened, trades_closed=result.trades_closed, duration_ms=result.duration_ms, error=result.error)
        self.store.set_state("last_cycle", {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": result.error,
                                            "markets": result.markets_scanned, "signals": len(result.trade_signals)})
        return result

    def run_forever(self, interval: int | None = None, max_cycles: int | None = None) -> None:
        interval = interval or self.s.scan_interval_seconds
        n = 0
        log.info("KalshiTrader running: mode=%s env=%s interval=%ss", self.mode, self.s.kalshi_env, interval)
        while True:
            res = self.cycle()
            log.info("cycle done: %d markets, %d trade signals, %d opened, %d closed, %dms%s",
                     res.markets_scanned, len(res.trade_signals), res.trades_opened, res.trades_closed, res.duration_ms,
                     f" ERROR {res.error}" if res.error else "")
            n += 1
            if max_cycles is not None and n >= max_cycles:
                return
            time.sleep(max(1, interval - res.duration_ms / 1000))


def _snapshot_dict(m: Market) -> dict:
    return {"ticker": m.ticker, "yes_bid": m.yes_bid, "yes_ask": m.yes_ask, "no_bid": m.no_bid, "no_ask": m.no_ask,
            "last_price": m.last_price, "volume_24h": m.volume_24h, "open_interest": m.open_interest, "volume": m.volume}
