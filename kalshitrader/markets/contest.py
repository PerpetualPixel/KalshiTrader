"""Turn raw Kalshi markets into `Contest` objects the strategy can trade.

A contest is one thing you can take a position on, with one `Leg` per side saying
which ticker and which side of it to buy. Kalshi lists the same idea in several
shapes, and all of them collapse to that:

  head-to-head, split   an event with one market per competitor, each market's
                        Yes meaning "this one wins" (most sports)
  head-to-head, single  one "A vs B" market, Yes = A, No = B
  single outcome        one "Will X happen?" market, Yes = it happens, No = it does not
  multi outcome         an event with many thresholds - a price strip, a range of
                        temperatures, a field of candidates. Each threshold is its
                        own contest, because they are separate bets that happen to
                        share an event.

Nothing here knows about tennis, or about any particular sport. Which series get
scanned is decided by the enabled-series list, which is discovered from the
exchange rather than hard-coded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from kalshitrader.kalshi.models import Market, Orderbook

VS_PATTERN = re.compile(r"^\s*(?:will\s+)?(.+?)\s+(?:vs\.?|v\.?|versus)\s+(.+?)\s*(?:\?|:|-|–|—|$|\(|match|winner|game)", re.IGNORECASE)
WIN_PATTERN = re.compile(r"^\s*will\s+(.+?)\s+(?:win|beat|defeat)\b", re.IGNORECASE)
NO_SIDE = "No"


@dataclass
class Leg:
    """One side of a contest: which ticker to buy, and which side of it."""

    name: str
    ticker: str
    side: str  # "yes" | "no"
    market: Market
    book: Orderbook | None = None  # attached by the engine; authoritative when set

    def attach(self, book: Orderbook | None) -> None:
        """Prices come from the order book when we have one.

        The /markets list response does not always carry live quotes (we have seen
        yes_bid/yes_ask arrive as 0 on in-play markets), so the book is preferred
        whenever it is available and the list prices are missing.
        """
        self.book = book

    @property
    def ask(self) -> int:
        listed = self.market.yes_ask if self.side == "yes" else self.market.no_ask
        if listed > 0:
            return listed
        if self.book is not None:
            best = self.book.best_yes_ask if self.side == "yes" else self.book.best_no_ask
            if best:
                return best
        return 0

    @property
    def bid(self) -> int:
        listed = self.market.yes_bid if self.side == "yes" else self.market.no_bid
        if listed > 0:
            return listed
        if self.book is not None:
            best = self.book.best_yes_bid if self.side == "yes" else self.book.best_no_bid
            if best:
                return best
        return 0

    @property
    def spread(self) -> int:
        if self.ask > 0 and self.bid > 0:
            return self.ask - self.bid
        return 100

    # The research modules were written against tennis and still say "player".
    @property
    def player(self) -> str:
        return self.name


@dataclass
class Contest:
    """One tradeable event, with a leg per side."""

    key: str  # stable id, the event ticker when available
    a: str  # the name of the side a "yes" buys
    b: str  # the opposing side
    legs: dict[str, Leg]
    event: str = ""  # tournament, league or series label, for display
    close_time: datetime | None = None
    markets: list[Market] = field(default_factory=list)
    series: str = ""

    @property
    def title(self) -> str:
        return f"{self.a} vs {self.b}" if self.b != NO_SIDE else self.a

    # Aliases: the tennis research and form modules are genuinely about players and
    # read these names. Everything generic uses `a`/`b`.
    @property
    def player_a(self) -> str:
        return self.a

    @property
    def player_b(self) -> str:
        return self.b

    @property
    def players(self) -> tuple[str, str]:
        return self.a, self.b

    @property
    def tournament(self) -> str:
        return self.event

    @property
    def sides(self) -> tuple[str, str]:
        return self.a, self.b

    def leg(self, name: str) -> Leg:
        return self.legs[name]

    def opponent(self, name: str) -> str:
        return self.b if name == self.a else self.a

    @property
    def volume_24h(self) -> int:
        return sum(m.volume_24h for m in self.markets)

    def is_open(self) -> bool:
        return all(m.is_tradeable for m in self.markets)

    def settled_winner(self) -> str | None:
        for name, leg in self.legs.items():
            m = leg.market
            if m.is_settled() and m.result == leg.side:
                return name
        return None


def _clean(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" ?.:-")
    name = re.sub(r"\s*\((?:[^)]*)\)\s*$", "", name)  # trailing seeds like "(3)"
    return name


def parse_sides(title: str) -> tuple[str, str] | None:
    m = VS_PATTERN.match(title)
    if m:
        a, b = _clean(m.group(1)), _clean(m.group(2))
        if a and b and a.lower() != b.lower():
            return a, b
    return None


def parse_single(title: str) -> str | None:
    m = WIN_PATTERN.match(title)
    return _clean(m.group(1)) if m else None


def in_scope(market: Market, series: list[str], prefixes: list[str]) -> bool:
    """Is this market one of the series we were told to trade?"""
    ticker = market.series_ticker.upper()
    event = market.event_ticker.upper()
    if any(ticker == s.upper() for s in series):
        return True
    return any(ticker.startswith(p.upper()) or event.startswith(p.upper()) for p in prefixes)


def find_contests(markets: list[Market], series: list[str], prefixes: list[str] | None = None) -> list[Contest]:
    """Group the markets we are allowed to trade into contests."""
    prefixes = prefixes or []
    scoped = [m for m in markets if in_scope(m, series, prefixes)]
    by_event: dict[str, list[Market]] = {}
    for m in scoped:
        by_event.setdefault(m.event_ticker or m.ticker, []).append(m)

    contests: list[Contest] = []
    for event, group in by_event.items():
        contests.extend(_contests_from_group(event, group))
    return contests


def _contests_from_group(event: str, group: list[Market]) -> list[Contest]:
    close = min((m.close_time for m in group if m.close_time), default=None)
    label = _event_label(group[0])
    series = group[0].series_ticker

    # Shape 1: exactly two markets, each naming its side. Yes on each = that side wins.
    named = [(m.subtitle.strip(), m) for m in group if m.subtitle.strip()]
    if len(named) == 2 and named[0][0].lower() != named[1][0].lower():
        (a, ma), (b, mb) = named
        a, b = _clean(a), _clean(b)
        legs = {a: Leg(a, ma.ticker, "yes", ma), b: Leg(b, mb.ticker, "yes", mb)}
        return [Contest(event, a, b, legs, label, close, group, series)]

    # Shape 2: a single "A vs B" market, Yes = A, No = B.
    if len(group) == 1:
        m = group[0]
        pair = parse_sides(m.title) or parse_sides(m.subtitle)
        if pair:
            a, b = pair
            legs = {a: Leg(a, m.ticker, "yes", m), b: Leg(b, m.ticker, "no", m)}
            return [Contest(event, a, b, legs, label, close, [m], series)]

    # Shapes 3 and 4: every remaining market is its own yes/no bet. One of them is a
    # plain "will X happen?"; many of them is a strip of thresholds or a field of
    # candidates, which are separate bets that merely share an event ticker.
    return [_binary_contest(event if len(group) == 1 else m.ticker, m, label, close, series) for m in group]


def _binary_contest(key: str, m: Market, label: str, close: datetime | None, series: str) -> Contest:
    a = _clean(m.subtitle) or parse_single(m.title) or _clean(m.title) or m.ticker
    legs = {a: Leg(a, m.ticker, "yes", m), NO_SIDE: Leg(NO_SIDE, m.ticker, "no", m)}
    return Contest(key, a, NO_SIDE, legs, label, m.close_time or close, [m], series)


def _event_label(m: Market) -> str:
    """A short human label for where this contest lives."""
    return m.series_ticker or m.event_ticker
