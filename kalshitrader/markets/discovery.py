"""Find out what is actually trading on Kalshi, and rank it.

There is no hard-coded list of series here on purpose. Ticker names are exactly the
sort of thing that is easy to half-remember and wrong, and a wrong list is invisible
until nothing trades. So the bot pages the exchange's open markets, groups them by
series, and reports what it found with the volume behind it. You switch on the ones
you want from that list, and the dashboard shows the same numbers it ranked them by.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from kalshitrader.kalshi.models import Market

log = logging.getLogger(__name__)

# Grouping for display only. A series that matches nothing lands in "Other" and is
# still perfectly tradeable - these patterns decide which heading it appears under,
# never whether it can be used.
CATEGORIES: tuple[tuple[str, re.Pattern], ...] = (
    ("Tennis", re.compile(r"^(?:ATP|WTA|TENNIS)", re.I)),
    ("Football", re.compile(r"^(?:NFL|NCAAF|CFB|SUPERBOWL)", re.I)),
    ("Basketball", re.compile(r"^(?:NBA|NCAAB|WNBA)", re.I)),
    ("Baseball", re.compile(r"^(?:MLB|WORLDSERIES)", re.I)),
    ("Hockey", re.compile(r"^(?:NHL|STANLEY)", re.I)),
    ("Soccer", re.compile(r"^(?:EPL|UCL|MLS|SOCCER|FIFA|LALIGA|SERIEA|BUNDES)", re.I)),
    ("Golf & combat", re.compile(r"^(?:PGA|GOLF|UFC|BOX)", re.I)),
    ("Crypto", re.compile(r"^(?:BTC|ETH|SOL|XRP|DOGE|CRYPTO)", re.I)),
    ("Economics", re.compile(r"^(?:FED|CPI|GDP|PAYROLL|JOBS|INFL|RATE|UNEMP|RECESS)", re.I)),
    ("Weather", re.compile(r"^(?:HIGH|LOW|TEMP|RAIN|SNOW|HURRICANE)", re.I)),
    ("Politics", re.compile(r"^(?:PRES|SENATE|HOUSE|ELECT|GOV|POLL|NOMINEE)", re.I)),
    ("Companies & media", re.compile(r"^(?:ROTTEN|OSCAR|GRAMMY|BOXOFFICE|IPO|EARN|TESLA|APPLE)", re.I)),
)


def categorise(series_ticker: str) -> str:
    """Which heading a series appears under.

    Matched against the start of the ticker with its "KX" prefix stripped, not
    anywhere inside it: a loose substring search files KXSOMETHINGNEW under Crypto,
    because "somETHing" contains ETH.
    """
    body = re.sub(r"^KX", "", series_ticker.upper())
    for name, pattern in CATEGORIES:
        if pattern.match(body):
            return name
    return "Other"


@dataclass
class SeriesInfo:
    """One tradeable series, as the exchange currently shows it."""

    ticker: str
    category: str
    markets: int = 0
    open_markets: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    liquid_markets: int = 0  # markets with a two-sided quote, i.e. actually tradeable now
    examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker, "category": self.category, "markets": self.markets,
            "open_markets": self.open_markets, "volume_24h": self.volume_24h,
            "open_interest": self.open_interest, "liquid_markets": self.liquid_markets,
            "examples": self.examples[:3],
        }


def summarise(markets: list[Market]) -> list[SeriesInfo]:
    """Group markets by series and rank them by the volume actually behind them."""
    found: dict[str, SeriesInfo] = {}
    for m in markets:
        ticker = m.series_ticker or m.event_ticker.split("-")[0]
        if not ticker:
            continue
        info = found.get(ticker)
        if info is None:
            info = found[ticker] = SeriesInfo(ticker=ticker, category=categorise(ticker))
        info.markets += 1
        info.volume_24h += m.volume_24h
        info.open_interest += m.open_interest
        if m.is_tradeable:
            info.open_markets += 1
            if m.yes_bid > 0 and m.yes_ask > 0:
                info.liquid_markets += 1
        if len(info.examples) < 3 and m.title:
            info.examples.append(m.title[:70])
    # Volume first: "big and profitable" starts with somewhere you can actually get
    # filled, and a series with no two-sided quotes is untradeable however many
    # markets it lists.
    return sorted(found.values(), key=lambda s: (-s.volume_24h, -s.liquid_markets, s.ticker))


def by_category(series: list[SeriesInfo]) -> dict[str, list[SeriesInfo]]:
    grouped: dict[str, list[SeriesInfo]] = {}
    for s in series:
        grouped.setdefault(s.category, []).append(s)
    return dict(sorted(grouped.items(), key=lambda kv: -sum(s.volume_24h for s in kv[1])))


def sweep(client, pages: int = 12, limit: int = 1000) -> list[SeriesInfo]:
    """Page the exchange's open markets and report every series found, ranked.

    This is the only honest way to answer "what can I trade?": ticker names guessed
    from memory look exactly like a market with nothing in it, and the difference is
    invisible until a whole session passes without a single trade.
    """
    markets = client.get_markets(limit=limit, max_pages=pages)
    series = summarise(markets)
    log.info("discovery: %d markets across %d series", len(markets), len(series))
    return series
