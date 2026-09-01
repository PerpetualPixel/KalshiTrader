from src.strategies.swing_trader import (
    LOOKBACK_SECONDS,
    exit_reason,
    swing_drop,
)


def test_swing_drop_measures_fall_from_recent_high():
    now = 1000.0
    history = [(now - 200, 60), (now - 100, 55), (now - 50, 52)]
    assert swing_drop(history, now, 48) == 12  # 60 -> 48


def test_swing_drop_ignores_prices_outside_lookback():
    now = 10_000.0
    history = [(now - LOOKBACK_SECONDS - 60, 90), (now - 60, 55)]
    assert swing_drop(history, now, 50) == 5  # the 90 is stale


def test_swing_drop_empty_history_is_zero():
    assert swing_drop([], 1000.0, 50) == 0


def test_exit_take_profit():
    assert exit_reason(50, 56, held_seconds=60, take_profit_cents=5,
                       stop_loss_cents=8, max_hold_seconds=1800) == "take_profit"


def test_exit_stop_loss():
    assert exit_reason(50, 42, held_seconds=60, take_profit_cents=5,
                       stop_loss_cents=8, max_hold_seconds=1800) == "stop_loss"


def test_exit_max_hold():
    assert exit_reason(50, 51, held_seconds=1801, take_profit_cents=5,
                       stop_loss_cents=8, max_hold_seconds=1800) == "max_hold"


def test_no_exit_inside_bands():
    assert exit_reason(50, 52, held_seconds=60, take_profit_cents=5,
                       stop_loss_cents=8, max_hold_seconds=1800) is None
