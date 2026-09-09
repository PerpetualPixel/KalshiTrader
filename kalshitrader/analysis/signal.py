"""The Signal: one decision about one market, and its 4-line wire format.

The 4-line template is the bot's only human-facing output for a decision:

    SIGNAL: [WIDE SPREAD / ILLIQUID] BUY YES @ 42c x10 | TICKER
    EDGE:   P_true=0.55 (conf 0.70) | P_mkt=0.42 | EV=+13.0c gross / +11.3c net | spread=2c
    EXIT:   TP 52c | SL 34c | closes 2026-09-08T00:00Z
    WHY:    <one-line rationale>
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum

WIDE_SPREAD_FLAG = "[WIDE SPREAD / ILLIQUID]"


class SignalAction(str, Enum):
    BUY_YES = "BUY YES"
    BUY_NO = "BUY NO"
    PASS = "PASS"
    HOLD = "HOLD"


@dataclass
class Signal:
    ticker: str
    title: str
    action: SignalAction
    entry_price: int  # cents; the ask we would lift (0 for PASS)
    p_true: float
    p_market: float
    confidence: float
    ev_gross: float  # cents per contract
    ev_net: float  # cents per contract after entry fee
    spread: int  # cents on the side we would trade
    depth: int  # contracts resting at the ask we would lift
    take_profit: int = 0  # cents
    stop_loss: int = 0  # cents
    size: int = 0  # contracts
    rationale: str = ""
    flags: list[str] = field(default_factory=list)
    close_time: datetime | None = None
    estimator: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def side(self) -> str | None:
        if self.action == SignalAction.BUY_YES:
            return "yes"
        if self.action == SignalAction.BUY_NO:
            return "no"
        return None

    @property
    def is_trade(self) -> bool:
        return self.action in (SignalAction.BUY_YES, SignalAction.BUY_NO)

    @property
    def wide_spread(self) -> bool:
        return WIDE_SPREAD_FLAG in self.flags

    def format(self) -> str:
        prefix = f"{WIDE_SPREAD_FLAG} " if self.wide_spread else ""
        if self.is_trade:
            line1 = f"SIGNAL: {prefix}{self.action.value} @ {self.entry_price}c x{self.size} | {self.ticker}"
            closes = self.close_time.strftime("%Y-%m-%dT%H:%MZ") if self.close_time else "n/a"
            line3 = f"EXIT:   TP {self.take_profit}c | SL {self.stop_loss}c | closes {closes}"
        else:
            line1 = f"SIGNAL: {prefix}{self.action.value} | {self.ticker}"
            line3 = "EXIT:   n/a"
        line2 = (
            f"EDGE:   P_true={self.p_true:.2f} (conf {self.confidence:.2f}) | P_mkt={self.p_market:.2f}"
            f" | EV={self.ev_gross:+.1f}c gross / {self.ev_net:+.1f}c net | spread={self.spread}c"
        )
        line4 = f"WHY:    {self.rationale or '-'}"
        return "\n".join((line1, line2, line3, line4))

    def to_record(self) -> dict:
        d = asdict(self)
        d["action"] = self.action.value
        d["side"] = self.side
        d["flags"] = ",".join(self.flags)
        d["close_time"] = self.close_time.isoformat() if self.close_time else None
        d["created_at"] = self.created_at.isoformat()
        d["formatted"] = self.format()
        return d
