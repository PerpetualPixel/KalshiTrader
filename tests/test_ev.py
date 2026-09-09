import math

import pytest

from kalshitrader.analysis.ev import (
    contracts_for_budget,
    expected_value_cents,
    implied_probability,
    kalshi_fee_cents,
    kelly_fraction,
    net_expected_value_cents,
)


def test_implied_probability_is_price_over_100():
    assert implied_probability(42) == pytest.approx(0.42)
    assert implied_probability(150) == 1.0
    assert implied_probability(-5) == 0.0


def test_expected_value_matches_formula():
    # EV = P_true*(100-entry) - (1-P_true)*entry
    assert expected_value_cents(0.55, 42) == pytest.approx(0.55 * 58 - 0.45 * 42)
    assert expected_value_cents(0.42, 42) == pytest.approx(0.0)
    assert expected_value_cents(0.30, 42) < 0


def test_fee_is_seven_percent_of_p_times_one_minus_p():
    assert kalshi_fee_cents(50) == pytest.approx(1.75)
    assert kalshi_fee_cents(50, count=10, round_up=True) == 18.0  # 17.5c rounds up
    assert kalshi_fee_cents(10) == pytest.approx(0.63)
    assert kalshi_fee_cents(50, fee_rate=0.0) == 0.0


def test_net_ev_subtracts_fee():
    assert net_expected_value_cents(0.55, 42) == pytest.approx(expected_value_cents(0.55, 42) - kalshi_fee_cents(42))


def test_kelly_fraction_bounds():
    assert kelly_fraction(0.5, 50) == pytest.approx(0.0)
    assert kelly_fraction(0.6, 50) == pytest.approx(0.2)
    assert kelly_fraction(0.3, 50) == 0.0
    assert kelly_fraction(0.99, 1) <= 1.0
    assert kelly_fraction(0.5, 0) == 0.0
    assert kelly_fraction(0.5, 100) == 0.0


def test_contracts_for_budget_floors():
    assert contracts_for_budget(10.0, 42) == math.floor(1000 / 42)
    assert contracts_for_budget(0.0, 42) == 0
    assert contracts_for_budget(10.0, 0) == 0
