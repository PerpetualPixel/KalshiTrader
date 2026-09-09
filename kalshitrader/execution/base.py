from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Fill:
    ticker: str
    side: str
    action: str  # buy | sell
    price: int  # cents, average fill
    count: int
    fees_dollars: float
    broker_order_id: str | None = None
    status: str = "filled"  # filled | partial | resting | rejected


class Broker(Protocol):
    mode: str

    def cash_dollars(self) -> float: ...

    def buy(self, ticker: str, side: str, price: int, count: int) -> Fill: ...

    def sell(self, ticker: str, side: str, price: int, count: int) -> Fill: ...

    def cancel_stale(self, max_age_seconds: int) -> int: ...
