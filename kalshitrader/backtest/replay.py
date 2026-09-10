"""Replay the prices the bot already recorded, through the strategy it actually runs.

Every strategy question - is 14c the right dip, does a stop help, is riding to
settlement better - is answerable from the snapshots already in the database, in
seconds, instead of an hour of real trading. This runs the same `SwingStrategy` and
the same fee arithmetic as the live loop, so the answers are about the strategy and
not about a second implementation of it that drifted.

What it is honest about:

  * snapshots record prices, not titles or series, so the replay works one ticker at
    a time. That matches how the swing rules decide anyway - the dip, the stabilisation
    and the value gates all look at a single leg's own history - but it means research
    and form assessments are not replayed. Price-only entries are exactly what is
    modelled, which is the mode this bot runs in.
  * fills are taken at the quoted ask and bid with the paper broker's slippage. That
    is optimistic on thin books: a real order can move the price it is filling at.
  * it can only replay markets the bot was watching. A dip in a market that was
    switched off was never recorded, so it cannot be tested.
  * a replay that buys nothing looks exactly like a replay that is broken, so it
    never just reports zero: every pass is tallied by which gate turned it away, and
    `data_profile` reads the same history with no gates at all. Rules too tight and
    prices that never moved are then two different answers rather than one silence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from kalshitrader.analysis.blockers import ranked as ranked_blockers
from kalshitrader.analysis.blockers import tally as tally_blocker
from kalshitrader.analysis.ev import kalshi_fee_cents
from kalshitrader.config import Settings
from kalshitrader.kalshi.models import Market
from kalshitrader.markets.contest import NO_SIDE, Contest, Leg
from kalshitrader.risk.rules import PortfolioState, RiskManager
from kalshitrader.trading.strategy import SwingStrategy

log = logging.getLogger(__name__)


class ReplayStore:
    """The price history as it looked at one moment, plus the trades made so far.

    The strategy only ever asks a store two things - a ticker's snapshots and when it
    was last closed out - so this is the whole surface it needs.
    """

    def __init__(self, history: dict[str, list[dict]]):
        self.history = history
        self.now = ""  # ISO timestamp of the simulated clock
        self.exits: dict[str, str] = {}

    def snapshots(self, ticker: str, limit: int = 500) -> list[dict]:
        rows = [r for r in self.history.get(ticker, []) if r["ts"] <= self.now]
        return rows[-limit:]

    def last_exit_at(self, ticker: str, mode: str | None = None) -> str | None:
        return self.exits.get(ticker)


@dataclass
class PendingOrder:
    """A maker order posted into the book, waiting for someone to trade against it."""

    ticker: str
    side: str
    price: int
    count: int
    posted_at: str
    volume_at_post: int
    take_profit: int
    stop_loss: int
    p_true: float


@dataclass
class BacktestResult:
    settings: dict
    trades: list[dict] = field(default_factory=list)
    tickers: int = 0
    snapshots: int = 0
    span_hours: float = 0.0
    skipped_no_history: int = 0
    posted: int = 0  # maker orders placed
    unfilled: int = 0  # maker orders that expired without a counterparty
    evaluations: int = 0  # legs the strategy was asked to judge
    blockers: dict[str, dict] = field(default_factory=dict)  # why it passed, grouped

    @property
    def why_nothing(self) -> list[dict]:
        """The reasons the strategy passed, commonest first."""
        return ranked_blockers(self.blockers)

    @property
    def fill_rate(self) -> float | None:
        return (self.posted - self.unfilled) / self.posted if self.posted else None

    @property
    def closed(self) -> list[dict]:
        return [t for t in self.trades if t["exit_price"] is not None]

    @property
    def wins(self) -> list[dict]:
        return [t for t in self.closed if t["pnl"] > 0]

    @property
    def losses(self) -> list[dict]:
        return [t for t in self.closed if t["pnl"] <= 0]

    @property
    def pnl(self) -> float:
        return sum(t["pnl"] for t in self.closed)

    @property
    def fees(self) -> float:
        return sum(t["fees"] for t in self.closed)

    @property
    def win_rate(self) -> float:
        return len(self.wins) / len(self.closed) if self.closed else 0.0

    @property
    def expectancy(self) -> float:
        return self.pnl / len(self.closed) if self.closed else 0.0

    @property
    def profit_factor(self) -> float | None:
        lost = -sum(t["pnl"] for t in self.losses)
        won = sum(t["pnl"] for t in self.wins)
        if lost > 0:
            return won / lost
        return None  # undefined until something loses

    @property
    def break_even_win_rate(self) -> float | None:
        """The win rate this configuration needs just to stand still.

        Measured from what the trades actually returned, not from the configured
        target and stop, because fees and early exits move both.
        """
        if not self.wins or not self.losses:
            return None
        avg_win = sum(t["pnl"] for t in self.wins) / len(self.wins)
        avg_loss = -sum(t["pnl"] for t in self.losses) / len(self.losses)
        return avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) else None

    @property
    def open_at_end(self) -> int:
        return len(self.trades) - len(self.closed)

    def summary(self) -> dict:
        return {
            "trades": len(self.closed), "open_at_end": self.open_at_end,
            "win_rate": self.win_rate, "pnl": self.pnl, "fees": self.fees,
            "expectancy": self.expectancy, "profit_factor": self.profit_factor,
            "break_even_win_rate": self.break_even_win_rate,
            "tickers": self.tickers, "snapshots": self.snapshots, "span_hours": self.span_hours,
        }


def load_history(store, *, since: str | None = None, tickers: list[str] | None = None) -> dict[str, list[dict]]:
    """Every recorded snapshot, grouped by ticker and ordered oldest first."""
    rows = store.all_snapshots(since=since) if hasattr(store, "all_snapshots") else []
    history: dict[str, list[dict]] = {}
    keep = {t.upper() for t in tickers} if tickers else None
    for r in rows:
        if keep and r["ticker"].upper() not in keep:
            continue
        history.setdefault(r["ticker"], []).append(dict(r))
    for rows_for in history.values():
        rows_for.sort(key=lambda r: r["ts"])
    return history


def _market_from(snapshot: dict, ticker: str) -> Market:
    """A Market carrying the prices we recorded and nothing we did not."""
    return Market.from_api({
        "ticker": ticker, "event_ticker": ticker, "series_ticker": ticker.split("-")[0],
        "title": ticker, "yes_sub_title": "", "status": "active",
        "yes_bid": snapshot["yes_bid"], "yes_ask": snapshot["yes_ask"],
        "no_bid": snapshot["no_bid"], "no_ask": snapshot["no_ask"],
        "last_price": snapshot["last_price"], "volume_24h": snapshot.get("volume_24h") or 0,
        "open_interest": snapshot.get("open_interest") or 0, "volume": snapshot.get("volume") or 0,
    })


def run_backtest(store, settings: Settings, *, since: str | None = None, tickers: list[str] | None = None,
                 history: dict | None = None, maker: bool = False, maker_ttl_seconds: int = 300) -> BacktestResult:
    """Replay recorded prices through the strategy with these settings.

    `maker=True` models posting at the bid and selling at the ask instead of crossing
    the spread. That saves the spread on both sides, which at these prices is worth
    about as much as the whole net edge - but a posted order only fills when someone
    trades against it, so it is modelled rather than assumed:

        a buy posted at B fills when a later snapshot, inside the TTL, shows the bid
        at or below B *and* the cumulative volume counter has moved - somebody
        actually traded while the price was at your level.

    That is a proxy, not a queue simulation: it cannot see how much size was ahead of
    you at the same price, so it is optimistic. Read the fill rate alongside the P&L.
    """
    history = history if history is not None else load_history(store, since=since, tickers=tickers)
    result = BacktestResult(settings=_settings_digest(settings))
    if not history:
        return result

    replay = ReplayStore(history)
    strategy = SwingStrategy(settings, replay, RiskManager(settings))
    stamps = sorted({r["ts"] for rows in history.values() for r in rows})
    result.tickers, result.snapshots = len(history), sum(len(v) for v in history.values())
    result.span_hours = _hours_between(stamps[0], stamps[-1])

    cash = settings.paper_starting_cash
    open_trades: dict[str, dict] = {}
    pending: dict[str, PendingOrder] = {}

    for stamp in stamps:
        replay.now = stamp
        now = _parse(stamp)
        priced = {t: rows[-1] for t, rows in ((t, replay.snapshots(t, limit=1)) for t in history) if rows}

        # Resting maker orders: fill the ones the market came to, expire the rest.
        for ticker, order in list(pending.items()):
            snap = priced.get(ticker)
            if snap is None:
                continue
            bid = snap["yes_bid"] if order.side == "yes" else snap["no_bid"]
            traded = (snap.get("volume") or 0) > order.volume_at_post
            if bid and bid <= order.price and traded:
                trade = _from_pending(order, stamp, settings)
                cost = trade["entry_price"] * trade["count"] / 100 + trade["fees"]
                del pending[ticker]
                if cost > cash:
                    continue
                cash -= cost
                open_trades[ticker] = trade
                result.trades.append(trade)
            elif (_parse(stamp) - _parse(order.posted_at)).total_seconds() > maker_ttl_seconds:
                # Nobody traded at our price inside the window. A real order would be
                # cancelled here rather than left resting into a moved market.
                del pending[ticker]
                result.unfilled += 1

        # Exits first, exactly as the live loop orders them: a position that should
        # have closed must not still be counted against the exposure cap.
        for ticker, trade in list(open_trades.items()):
            snap = priced.get(ticker)
            if snap is None:
                continue
            bid = snap["yes_bid"] if trade["side"] == "yes" else snap["no_bid"]
            reason = strategy.risk.exit_reason(trade["side"], trade["entry_price"], trade["take_profit"],
                                               trade["stop_loss"], bid, trade["p_true"])
            if not reason:
                continue
            # A maker exit rests at the ask; the taker exit hits the bid.
            out = (snap["yes_ask"] if trade["side"] == "yes" else snap["no_ask"]) if maker else bid
            cash += _close(trade, out or bid, reason, stamp, settings, maker=maker)
            replay.exits[ticker] = stamp
            del open_trades[ticker]

        exposure = sum(t["entry_price"] * t["count"] / 100 for t in open_trades.values())
        state = PortfolioState(
            cash_dollars=cash, exposure_dollars=exposure, open_positions=len(open_trades),
            daily_pnl_dollars=0.0, consecutive_losses=0,
            open_tickers=set(open_trades), halted=False,
        )
        for ticker, snap in priced.items():
            if ticker in open_trades or ticker in pending:
                continue
            contest, leg_name = _contest_for(ticker, snap)
            signal = strategy.evaluate_leg(contest, leg_name, contest.legs[leg_name], None, None, state, now)
            result.evaluations += 1
            if not signal.is_trade:
                # A replay that buys nothing and a replay that is broken look identical
                # from the outside. Record which gate turned each candidate away.
                tally_blocker(result.blockers, signal.rationale, ticker)
                continue
            if maker:
                # Post at the bid rather than crossing to the ask. Nothing is owned
                # until somebody trades against it, so no cash moves yet.
                post_at = max(1, signal.entry_price - signal.spread)
                pending[ticker] = PendingOrder(ticker, "yes" if signal.action.value.endswith("YES") else "no",
                                               post_at, signal.size, stamp, snap.get("volume") or 0,
                                               signal.take_profit, signal.stop_loss, signal.p_true)
                result.posted += 1
                continue
            trade = _open(signal, ticker, stamp, settings)
            cost = trade["entry_price"] * trade["count"] / 100 + trade["fees"]
            if cost > cash:
                continue
            cash -= cost
            open_trades[ticker] = trade
            result.trades.append(trade)
            exposure += trade["entry_price"] * trade["count"] / 100
            state = PortfolioState(cash_dollars=cash, exposure_dollars=exposure,
                                   open_positions=len(open_trades), daily_pnl_dollars=0.0,
                                   consecutive_losses=0, open_tickers=set(open_trades), halted=False)
    return result


def _contest_for(ticker: str, snap: dict) -> tuple[Contest, str]:
    """A one-market contest around a recorded ticker, so the real strategy can judge it."""
    market = _market_from(snap, ticker)
    name = ticker
    legs = {name: Leg(name, ticker, "yes", market), NO_SIDE: Leg(NO_SIDE, ticker, "no", market)}
    return Contest(ticker, name, NO_SIDE, legs, ticker.split("-")[0], market.close_time, [market], ticker.split("-")[0]), name


def _open(signal, ticker: str, stamp: str, s: Settings) -> dict:
    entry = min(99, signal.entry_price + s.paper_slippage_cents)
    return {
        "ticker": ticker, "side": "yes" if signal.action.value.endswith("YES") else "no",
        "entry_price": entry, "count": signal.size, "take_profit": signal.take_profit,
        "stop_loss": signal.stop_loss, "p_true": signal.p_true, "opened_at": stamp,
        "fees": kalshi_fee_cents(entry, s.fee_rate, signal.size, round_up=True) / 100,
        "exit_price": None, "exit_reason": None, "closed_at": None, "pnl": 0.0,
    }


def _from_pending(order: PendingOrder, stamp: str, s: Settings) -> dict:
    """A maker order that found a counterparty becomes a position at the price posted."""
    return {
        "ticker": order.ticker, "side": order.side, "entry_price": order.price, "count": order.count,
        "take_profit": order.take_profit, "stop_loss": order.stop_loss, "p_true": order.p_true,
        "opened_at": stamp, "fees": kalshi_fee_cents(order.price, s.fee_rate, order.count, round_up=True) / 100,
        "exit_price": None, "exit_reason": None, "closed_at": None, "pnl": 0.0,
    }


def _close(trade: dict, bid: int, reason: str, stamp: str, s: Settings, maker: bool = False) -> float:
    # A resting sell is filled at the price posted; crossing the spread pays slippage.
    exit_price = bid if maker else max(1, bid - s.paper_slippage_cents)
    exit_fee = kalshi_fee_cents(exit_price, s.fee_rate, trade["count"], round_up=True) / 100
    trade["exit_price"], trade["exit_reason"], trade["closed_at"] = exit_price, reason, stamp
    trade["fees"] += exit_fee
    proceeds = exit_price * trade["count"] / 100
    trade["pnl"] = proceeds - trade["entry_price"] * trade["count"] / 100 - trade["fees"]
    return proceeds - exit_fee


def _settings_digest(s: Settings) -> dict:
    return {"dip_cents": s.dip_cents, "take_profit_cents": s.take_profit_cents,
            "stop_loss_cents": s.stop_loss_cents, "use_stop_loss": s.use_stop_loss,
            "min_edge_cents": s.min_edge_cents, "require_stabilized": s.require_stabilized,
            "reentry_cooldown_minutes": s.reentry_cooldown_minutes,
            "max_position_dollars": s.max_position_dollars, "kelly_fraction": s.kelly_fraction}


def _parse(stamp: str) -> datetime:
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hours_between(a: str, b: str) -> float:
    return round((_parse(b) - _parse(a)).total_seconds() / 3600, 2)


def sweep_settings(store, base: Settings, variations: list[dict], *, since: str | None = None) -> list[BacktestResult]:
    """Run the same history through several configurations, so they can be compared.

    The history is loaded once and shared: reading it per run would dominate the time
    and, worse, invite comparing runs over subtly different spans.
    """
    from dataclasses import replace

    history = load_history(store, since=since)
    out = []
    for changes in variations:
        out.append(run_backtest(store, replace(base, **changes), history=history))
    return out


@dataclass
class DataProfile:
    """What the recorded prices contain, measured without reference to any strategy.

    When a replay returns no trades, the first question is whether the rules were too
    tight or the history simply holds nothing to trade. The blocker tally answers the
    first. This answers the second: it walks the same rolling-high arithmetic the dip
    gate uses, but applies no gates at all, so a flat hour of quotes and an hour full
    of dips the rules rejected are told apart.
    """

    tickers: int = 0
    quoted_tickers: int = 0  # had a two-sided quote at some point
    traded_tickers: int = 0  # cumulative volume counter moved
    deepest: list[tuple[str, int]] = field(default_factory=list)  # (ticker, deepest dip in cents)
    dips_at_least: dict[int, int] = field(default_factory=dict)  # cents -> tickers reaching it

    @property
    def max_dip(self) -> int:
        return max((d for _, d in self.deepest), default=0)


def data_profile(history: dict[str, list[dict]], *, window_minutes: int = 45,
                 buckets: tuple[int, ...] = (2, 4, 6, 8, 12, 20)) -> DataProfile:
    """Deepest dip, quotes and tape activity per ticker, from the snapshots alone."""
    profile = DataProfile(tickers=len(history))
    for ticker, rows in history.items():
        window: list[tuple[datetime, int]] = []
        deepest = 0
        quoted = False
        volumes = [r["volume"] for r in rows if r.get("volume") is not None]
        for row in rows:
            ask, bid = row.get("yes_ask") or 0, row.get("yes_bid") or 0
            if ask <= 0:
                continue
            quoted = quoted or bid > 0
            stamp = _parse(row["ts"])
            cutoff = stamp - timedelta(minutes=window_minutes)
            window.append((stamp, ask))
            window = [w for w in window if w[0] >= cutoff]
            deepest = max(deepest, max(a for _, a in window) - ask)
        profile.deepest.append((ticker, deepest))
        profile.quoted_tickers += quoted
        profile.traded_tickers += len(volumes) >= 2 and volumes[-1] > volumes[0]
    profile.deepest.sort(key=lambda p: -p[1])
    profile.dips_at_least = {c: sum(1 for _, d in profile.deepest if d >= c) for c in buckets}
    return profile
