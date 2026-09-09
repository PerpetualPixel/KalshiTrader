"""Live broker: places limit orders on Kalshi at the quoted price.

Orders are placed as limit-at-ask so they normally fill immediately. Anything
that rests is recorded and cancelled by `cancel_stale` on a later cycle, so a
stale limit never sits in the book for hours.
"""
from __future__ import annotations

import logging
import time
import uuid

from kalshitrader.analysis.ev import kalshi_fee_cents
from kalshitrader.execution.base import Fill
from kalshitrader.kalshi.client import KalshiClient, KalshiError
from kalshitrader.tracking.db import Store

log = logging.getLogger(__name__)


class LiveBroker:
    mode = "live"

    def __init__(self, client: KalshiClient, store: Store, fee_rate: float = 0.07, fill_wait_seconds: float = 2.0):
        if not client.authenticated:
            raise ValueError("LiveBroker requires an authenticated KalshiClient")
        self.client = client
        self.store = store
        self.fee_rate = fee_rate
        self.fill_wait = fill_wait_seconds

    def cash_dollars(self) -> float:
        return float(self.client.get_balance().get("balance", 0)) / 100.0

    def _place(self, ticker: str, side: str, action: str, price: int, count: int) -> Fill:
        self._clear_resting(ticker)
        client_order_id = str(uuid.uuid4())
        try:
            order = self.client.create_order(
                ticker=ticker, action=action, side=side, count=count, price_cents=price, client_order_id=client_order_id
            )
        except KalshiError as exc:
            log.error("order rejected for %s %s %s x%d @ %dc: %s", ticker, action, side, count, price, exc)
            return Fill(ticker, side, action, price, 0, 0.0, status="rejected")
        order_id = order.get("order_id") or order.get("id") or client_order_id
        # Give the matching engine a moment, then read back the fill state.
        time.sleep(self.fill_wait)
        try:
            order = self.client.get_order(order_id)
        except KalshiError:
            pass
        filled = int(order.get("fill_count") or order.get("filled_count") or 0)
        remaining = int(order.get("remaining_count") or max(0, count - filled))
        status = order.get("status", "resting")
        avg = int(order.get("yes_price" if side == "yes" else "no_price") or price)
        fees = float(order.get("taker_fees") or order.get("fees") or 0) / 100.0
        if fees == 0 and filled:
            fees = kalshi_fee_cents(avg, self.fee_rate, filled, round_up=True) / 100.0
        if filled == 0 and remaining > 0 and status not in ("executed", "canceled", "cancelled"):
            self.store.add_order(mode=self.mode, ticker=ticker, action=action, side=side, price=price, count=count,
                                 status="resting", broker_order_id=order_id)
            return Fill(ticker, side, action, avg, 0, 0.0, broker_order_id=order_id, status="resting")
        if remaining > 0:
            try:
                self.client.cancel_order(order_id)
            except KalshiError as exc:
                log.warning("could not cancel remainder of %s: %s", order_id, exc)
        return Fill(ticker, side, action, avg, filled, fees, broker_order_id=order_id,
                    status="filled" if remaining == 0 else "partial")

    def _clear_resting(self, ticker: str) -> None:
        """Cancel our own resting orders on this ticker first.

        Kalshi rejects an order that would trade against your own resting one, and a
        stale exit sitting in the book is exactly what a new entry would cross.
        """
        for o in self.store.pending_orders():
            if o["ticker"] != ticker or not o.get("broker_order_id"):
                continue
            try:
                self.client.cancel_order(o["broker_order_id"])
                self.store.update_order(o["id"], status="cancelled")
            except KalshiError as exc:
                self.store.update_order(o["id"], status="gone" if exc.status == 404 else o["status"])
                if exc.status != 404:
                    log.warning("could not cancel resting order %s on %s: %s", o["broker_order_id"], ticker, exc)

    def buy(self, ticker: str, side: str, price: int, count: int) -> Fill:
        return self._place(ticker, side, "buy", price, count)

    def sell(self, ticker: str, side: str, price: int, count: int) -> Fill:
        return self._place(ticker, side, "sell", price, count)

    def cancel_stale(self, max_age_seconds: int) -> int:
        cancelled = 0
        for o in self.store.pending_orders():
            if not o.get("broker_order_id"):
                continue
            try:
                self.client.cancel_order(o["broker_order_id"])
                self.store.update_order(o["id"], status="cancelled")
                cancelled += 1
            except KalshiError as exc:
                if exc.status == 404:
                    self.store.update_order(o["id"], status="gone")
                else:
                    log.warning("cancel failed for %s: %s", o["broker_order_id"], exc)
        return cancelled
