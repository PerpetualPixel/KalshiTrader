"""Pure pricing math. Everything here is deterministic and unit-tested.

Conventions: prices in integer cents (1-99), probabilities as floats in [0, 1].
"""
from __future__ import annotations

import math


def implied_probability(price_cents: float) -> float:
    """P_market = price / 100."""
    return max(0.0, min(1.0, price_cents / 100.0))


def expected_value_cents(p_true: float, entry_price_cents: float) -> float:
    """Gross EV per contract, in cents:

        EV = P_true * (100 - entry) - (1 - P_true) * entry
           = 100 * P_true - entry
    """
    return p_true * (100.0 - entry_price_cents) - (1.0 - p_true) * entry_price_cents


def kalshi_fee_cents(price_cents: float, fee_rate: float = 0.07, count: int = 1, round_up: bool = False) -> float:
    """Kalshi taker fee in cents for `count` contracts at `price_cents`.

    Kalshi charges fee_rate * C * P * (1 - P) dollars (P = price in dollars),
    rounded up to the next cent per order. Per contract at 50c that is 1.75c.
    `round_up=True` applies the per-order rounding; the default keeps the exact
    value, which is what EV-per-contract math wants.
    """
    p = price_cents / 100.0
    fee_cents = fee_rate * count * p * (1.0 - p) * 100.0
    return float(math.ceil(fee_cents - 1e-9)) if round_up else fee_cents


def net_expected_value_cents(p_true: float, entry_price_cents: float, fee_rate: float = 0.07) -> float:
    """EV after the entry fee. Exit-at-settlement pays no fee; we ignore the
    (optional) exit fee on early exits so the estimate stays conservative on
    the side that matters: we require a larger edge to enter, not a smaller one.
    """
    return expected_value_cents(p_true, entry_price_cents) - kalshi_fee_cents(entry_price_cents, fee_rate)


def kelly_fraction(p_true: float, entry_price_cents: float) -> float:
    """Full-Kelly fraction of bankroll for a binary contract bought at `entry`.

    Odds b = (100 - entry) / entry; f* = (p*b - q) / b. Clamped to [0, 1].
    """
    if entry_price_cents <= 0 or entry_price_cents >= 100:
        return 0.0
    b = (100.0 - entry_price_cents) / entry_price_cents
    q = 1.0 - p_true
    f = (p_true * b - q) / b
    return max(0.0, min(1.0, f))


def contracts_for_budget(budget_dollars: float, entry_price_cents: float) -> int:
    """How many contracts `budget_dollars` buys at `entry` (fees ignored, floor)."""
    if entry_price_cents <= 0:
        return 0
    return int(budget_dollars * 100 // entry_price_cents)


def pnl_cents(side: str, entry_price_cents: int, exit_price_cents: int, count: int) -> int:
    """Realised P&L for closing `count` contracts of `side` bought at entry and sold at exit."""
    return (exit_price_cents - entry_price_cents) * count


def settlement_value_cents(side: str, result: str) -> int:
    """Value of one contract at settlement: 100 if our side won, else 0."""
    return 100 if side == result else 0
