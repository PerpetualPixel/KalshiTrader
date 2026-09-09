"""Typed views over the Kalshi API payloads the bot actually uses.

All prices are integer cents (1-99). A Yes contract bought at price p pays 100
if the market settles Yes; a No contract bought at price q pays 100 if it
settles No. Kalshi quotes both sides, and `yes_ask == 100 - no_bid`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Kalshi's `status` **filter** on /markets takes "open", but the market objects
# themselves report "active" while trading is live. Treat both as tradeable, and
# keep the older/other spellings we have seen so a rename never silently stops
# the bot.
TRADEABLE_STATUSES = frozenset({"open", "active"})
SETTLED_STATUSES = frozenset({"settled", "finalized", "determined"})


def price_to_cents(value) -> int:
    """Coerce a Kalshi price to integer cents.

    Kalshi now reports prices as decimal dollar strings ("0.8800") alongside the
    older integer-cent fields (88). A string containing a decimal point, or a
    non-integral float, is dollars; anything else is already cents.
    """
    if value in (None, ""):
        return 0
    if isinstance(value, str):
        try:
            return int(round(float(value) * 100)) if "." in value else int(value)
        except ValueError:
            return 0
    if isinstance(value, float) and not value.is_integer():
        return int(round(value * 100))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _cents(d: dict, base: str) -> int:
    """Price in cents from `<base>_dollars` (preferred) or the legacy `<base>`."""
    if d.get(f"{base}_dollars") not in (None, ""):
        return price_to_cents(d[f"{base}_dollars"])
    return price_to_cents(d.get(base))


def _count(d: dict, base: str) -> int:
    """Contract count from `<base>_fp` (a decimal string) or the legacy `<base>`."""
    for key in (f"{base}_fp", base):
        raw = d.get(key)
        if raw in (None, ""):
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            continue
    return 0


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Market:
    ticker: str
    title: str
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    last_price: int
    volume: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    # Resting size at the top of book, as reported on the market itself. Used when
    # the order-book endpoint gives us nothing.
    yes_ask_size: int = 0
    no_ask_size: int = 0
    liquidity: int = 0  # dollars of resting liquidity (Kalshi reports cents; normalised below)
    status: str = "open"
    result: str = ""  # "", "yes", "no"
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    open_time: datetime | None = None
    # When play is scheduled to begin (`occurrence_datetime`). The only reliable
    # signal for whether a match has actually started.
    starts_at: datetime | None = None
    event_ticker: str = ""
    series_ticker: str = ""
    subtitle: str = ""
    rules_primary: str = ""
    category: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> Market:
        return cls(
            ticker=d.get("ticker", ""),
            title=d.get("title", ""),
            yes_bid=_cents(d, "yes_bid"),
            yes_ask=_cents(d, "yes_ask"),
            no_bid=_cents(d, "no_bid"),
            no_ask=_cents(d, "no_ask"),
            last_price=_cents(d, "last_price"),
            volume=_count(d, "volume"),
            volume_24h=_count(d, "volume_24h"),
            open_interest=_count(d, "open_interest"),
            yes_ask_size=_count(d, "yes_ask_size"),
            no_ask_size=_count(d, "no_ask_size"),
            liquidity=_cents(d, "liquidity"),
            status=d.get("status", "open"),
            result=d.get("result", "") or "",
            close_time=parse_ts(d.get("close_time")),
            expiration_time=parse_ts(d.get("expected_expiration_time") or d.get("expiration_time")),
            open_time=parse_ts(d.get("open_time")),
            starts_at=parse_ts(d.get("occurrence_datetime")),
            event_ticker=d.get("event_ticker", ""),
            series_ticker=d.get("series_ticker", "") or d.get("event_ticker", "").split("-")[0],
            subtitle=d.get("subtitle", "") or d.get("yes_sub_title", ""),
            rules_primary=d.get("rules_primary", ""),
            category=d.get("category", ""),
            raw=d,
        )

    def ask_size(self, side: str) -> int:
        return self.yes_ask_size if side == "yes" else self.no_ask_size

    def has_started(self, now: datetime | None = None) -> bool:
        """True when play is scheduled to have begun (unknown schedule = assume yes)."""
        if self.starts_at is None:
            return True
        return (now or datetime.now(timezone.utc)) >= self.starts_at

    @property
    def is_tradeable(self) -> bool:
        return self.status in TRADEABLE_STATUSES

    @property
    def has_quote(self) -> bool:
        return self.yes_ask > 0 and self.no_ask > 0

    @property
    def yes_spread(self) -> int:
        if self.yes_ask <= 0 or self.yes_bid <= 0:
            return 100
        return self.yes_ask - self.yes_bid

    @property
    def no_spread(self) -> int:
        if self.no_ask <= 0 or self.no_bid <= 0:
            return 100
        return self.no_ask - self.no_bid

    @property
    def yes_mid(self) -> float:
        if self.yes_ask > 0 and self.yes_bid > 0:
            return (self.yes_ask + self.yes_bid) / 2
        return float(self.last_price or 50)

    def hours_to_close(self, now: datetime | None = None) -> float | None:
        if self.close_time is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.close_time - now).total_seconds() / 3600

    def is_settled(self) -> bool:
        return self.status in SETTLED_STATUSES and self.result in ("yes", "no")


@dataclass
class Orderbook:
    """Resting bids per side. Each level is (price_cents, contracts)."""

    ticker: str
    yes: list[tuple[int, int]] = field(default_factory=list)
    no: list[tuple[int, int]] = field(default_factory=list)

    @staticmethod
    def _levels(raw) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for level in raw or []:
            try:
                price, size = level[0], level[1]
            except (TypeError, IndexError, KeyError):
                continue
            cents = price_to_cents(price)
            try:
                qty = int(float(size))
            except (TypeError, ValueError):
                qty = 0
            if cents > 0 and qty > 0:
                out.append((cents, qty))
        return out

    @classmethod
    def from_api(cls, ticker: str, d: dict) -> Orderbook:
        # Kalshi returns {"orderbook_fp": {"yes_dollars": [["0.87","50347.42"], ...]}}
        # and previously {"orderbook": {"yes": [[87, 50347], ...]}}. Accept both.
        ob = d.get("orderbook") or d.get("orderbook_fp") or d
        yes = cls._levels(ob.get("yes") or ob.get("yes_dollars"))
        no = cls._levels(ob.get("no") or ob.get("no_dollars"))
        yes.sort(key=lambda lvl: -lvl[0])
        no.sort(key=lambda lvl: -lvl[0])
        return cls(ticker=ticker, yes=yes, no=no)

    @property
    def best_yes_bid(self) -> int | None:
        return self.yes[0][0] if self.yes else None

    @property
    def best_no_bid(self) -> int | None:
        return self.no[0][0] if self.no else None

    @property
    def best_yes_ask(self) -> int | None:
        return 100 - self.no[0][0] if self.no else None

    @property
    def best_no_ask(self) -> int | None:
        return 100 - self.yes[0][0] if self.yes else None

    def depth_at_yes_ask(self) -> int:
        return self.no[0][1] if self.no else 0

    def depth_at_no_ask(self) -> int:
        return self.yes[0][1] if self.yes else 0

    def total_depth(self, side: str, levels: int = 3) -> int:
        book = self.yes if side == "yes" else self.no
        return sum(q for _, q in book[:levels])

    def imbalance(self) -> float:
        """(yes depth - no depth) / total, in [-1, 1]. Positive = buying pressure on Yes."""
        y = self.total_depth("yes")
        n = self.total_depth("no")
        if y + n == 0:
            return 0.0
        return (y - n) / (y + n)


@dataclass
class Position:
    ticker: str
    contracts: int  # +N long Yes, -N long No
    market_exposure_cents: int = 0
    realized_pnl_cents: int = 0
    resting_orders: int = 0

    @classmethod
    def from_api(cls, d: dict) -> Position:
        return cls(
            ticker=d.get("ticker", ""),
            contracts=int(d.get("position") or 0),
            market_exposure_cents=int(d.get("market_exposure") or 0),
            realized_pnl_cents=int(d.get("realized_pnl") or 0),
            resting_orders=int(d.get("resting_orders_count") or 0),
        )

    @property
    def side(self) -> str:
        return "yes" if self.contracts >= 0 else "no"
