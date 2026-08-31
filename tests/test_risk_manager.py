from src.config import BotSettings
from src.risk_manager import RiskManager


def make_rm(**overrides) -> RiskManager:
    defaults = dict(
        max_money_working_cents=1000,
        max_contracts_per_order=10,
        daily_stop_loss_pct=5.0,
    )
    defaults.update(overrides)
    return RiskManager(BotSettings(**defaults))


def test_allows_order_within_limits():
    rm = make_rm()
    decision = rm.check_order(count=5, price_cents=50, money_working_cents=0)
    assert decision.allowed


def test_blocks_oversized_order():
    rm = make_rm(max_contracts_per_order=3)
    decision = rm.check_order(count=4, price_cents=10, money_working_cents=0)
    assert not decision.allowed
    assert "max_contracts_per_order" in decision.reason


def test_blocks_when_capital_ceiling_exceeded():
    rm = make_rm(max_money_working_cents=1000)
    # 900c already working + 3 * 50c = 1050c > 1000c ceiling
    decision = rm.check_order(count=3, price_cents=50, money_working_cents=900)
    assert not decision.allowed
    assert "ceiling" in decision.reason


def test_exactly_at_ceiling_is_allowed():
    rm = make_rm(max_money_working_cents=1000)
    decision = rm.check_order(count=2, price_cents=50, money_working_cents=900)
    assert decision.allowed


def test_daily_stop_trips_and_halts_orders():
    rm = make_rm(daily_stop_loss_pct=5.0)
    tripped = rm.check_daily_stop(day_start_equity_cents=10000, equity_cents=9400)
    assert tripped
    assert rm.halted
    decision = rm.check_order(count=1, price_cents=10, money_working_cents=0)
    assert not decision.allowed
    assert "halted" in decision.reason


def test_daily_stop_not_tripped_within_tolerance():
    rm = make_rm(daily_stop_loss_pct=5.0)
    assert not rm.check_daily_stop(day_start_equity_cents=10000, equity_cents=9600)
    assert not rm.halted


def test_reset_clears_halt():
    rm = make_rm()
    rm.trip("test")
    rm.reset()
    assert not rm.halted
    assert rm.check_order(count=1, price_cents=10, money_working_cents=0).allowed
