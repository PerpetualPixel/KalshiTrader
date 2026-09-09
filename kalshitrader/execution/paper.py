"""Paper broker: fills immediately at the quoted price plus slippage, charges Kalshi fees."""
from __future__ import annotations

from kalshitrader.analysis.ev import kalshi_fee_cents
from kalshitrader.execution.base import Fill
from kalshitrader.tracking.db import Store


class PaperBroker:
    mode = "paper"

    def __init__(self, store: Store, starting_cash: float, slippage_cents: int = 1, fee_rate: float = 0.07):
        self.store = store
        self.slippage = slippage_cents
        self.fee_rate = fee_rate
        if self.store.get_state("paper_cash") is None:
            self.store.set_state("paper_cash", float(starting_cash))
            self.store.set_state("paper_starting_cash", float(starting_cash))

    def cash_dollars(self) -> float:
        return float(self.store.get_state("paper_cash", 0.0))

    def _adjust_cash(self, delta: float) -> None:
        self.store.set_state("paper_cash", round(self.cash_dollars() + delta, 4))

    def buy(self, ticker: str, side: str, price: int, count: int) -> Fill:
        fill_price = min(99, price + self.slippage)
        fees = kalshi_fee_cents(fill_price, self.fee_rate, count, round_up=True) / 100.0
        cost = fill_price * count / 100.0 + fees
        if cost > self.cash_dollars():
            affordable = int((self.cash_dollars() - fees) * 100 // fill_price)
            if affordable <= 0:
                return Fill(ticker, side, "buy", fill_price, 0, 0.0, status="rejected")
            count = affordable
            fees = kalshi_fee_cents(fill_price, self.fee_rate, count, round_up=True) / 100.0
            cost = fill_price * count / 100.0 + fees
        self._adjust_cash(-cost)
        return Fill(ticker, side, "buy", fill_price, count, fees)

    def sell(self, ticker: str, side: str, price: int, count: int) -> Fill:
        fill_price = max(1, price - self.slippage)
        fees = kalshi_fee_cents(fill_price, self.fee_rate, count, round_up=True) / 100.0
        self._adjust_cash(fill_price * count / 100.0 - fees)
        return Fill(ticker, side, "sell", fill_price, count, fees)

    def settle(self, count: int, won: bool) -> None:
        """Credit settlement proceeds (no fee on settlement)."""
        if won:
            self._adjust_cash(count * 1.0)

    def cancel_stale(self, max_age_seconds: int) -> int:
        return 0

