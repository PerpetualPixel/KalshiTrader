import pytest

from kalshitrader.analysis.estimators import Estimate
from kalshitrader.analysis.signal import WIDE_SPREAD_FLAG, SignalAction
from kalshitrader.kalshi.models import Market, Orderbook
from kalshitrader.risk.rules import PortfolioState, RiskManager
from tests.conftest import make_market


def state(**kw) -> PortfolioState:
    base = dict(cash_dollars=1000.0, exposure_dollars=0.0, open_positions=0, daily_pnl_dollars=0.0, consecutive_losses=0)
    base.update(kw)
    return PortfolioState(**base)


def book_for(m: Market, depth=200) -> Orderbook:
    return Orderbook.from_api(m.ticker, {"orderbook": {"yes": [[m.yes_bid, depth]], "no": [[m.no_bid, depth]]}})


def test_buy_yes_with_exits(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market(yes_bid=48, yes_ask=50))
    sig = rm.build_signal(m, book_for(m), Estimate(0.70, 0.8, "manual", "research"), state())
    assert sig.action == SignalAction.BUY_YES
    assert sig.entry_price == 50
    assert sig.take_profit == 60 and sig.stop_loss == 42
    assert sig.size > 0
    assert sig.ev_net == pytest.approx(70 - 50 - 1.75)
    assert "manual" in sig.rationale
    text = sig.format()
    assert text.splitlines()[0].startswith("SIGNAL: BUY YES @ 50c")
    assert len(text.splitlines()) == 4


def test_buy_no_when_market_overprices_yes(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market(yes_bid=68, yes_ask=70))
    sig = rm.build_signal(m, book_for(m), Estimate(0.40, 0.8, "manual"), state())
    assert sig.action == SignalAction.BUY_NO
    assert sig.entry_price == 32  # 100 - yes_bid


def test_pass_on_thin_edge(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market(yes_bid=48, yes_ask=50))
    sig = rm.build_signal(m, book_for(m), Estimate(0.53, 0.8, "manual"), state())
    assert sig.action == SignalAction.PASS
    assert "net edge" in sig.rationale


def test_wide_spread_flag_and_reject(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market(yes_bid=40, yes_ask=50))
    sig = rm.build_signal(m, book_for(m), Estimate(0.80, 0.9, "manual"), state())
    assert sig.action == SignalAction.PASS
    assert WIDE_SPREAD_FLAG in sig.flags
    assert sig.format().startswith(f"SIGNAL: {WIDE_SPREAD_FLAG} PASS")


def test_wide_spread_trades_when_allowed_but_keeps_flag(settings):
    settings.reject_wide_spread = False
    rm = RiskManager(settings)
    m = Market.from_api(make_market(yes_bid=40, yes_ask=50))
    sig = rm.build_signal(m, book_for(m), Estimate(0.80, 0.9, "manual"), state())
    assert sig.is_trade and sig.wide_spread
    assert sig.format().startswith(f"SIGNAL: {WIDE_SPREAD_FLAG} BUY YES")


def test_price_band(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market(yes_bid=96, yes_ask=97))
    sig = rm.build_signal(m, book_for(m), Estimate(0.999, 0.9, "manual"), state())
    assert sig.action == SignalAction.PASS and "band" in sig.rationale


def test_low_confidence_passes(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market())
    sig = rm.build_signal(m, book_for(m), Estimate(0.80, 0.2, "microstructure"), state())
    assert sig.action == SignalAction.PASS and "confidence" in sig.rationale


def test_circuit_breakers(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market())
    est = Estimate(0.80, 0.9, "manual")
    assert "daily loss" in rm.build_signal(m, None, est, state(daily_pnl_dollars=-60)).rationale
    assert "consecutive" in rm.build_signal(m, None, est, state(consecutive_losses=5)).rationale
    assert "halted" in rm.build_signal(m, None, est, state(halted=True)).rationale


def test_market_eligibility(settings):
    rm = RiskManager(settings)
    assert rm.market_eligible(Market.from_api(make_market(hours_to_close=0.1))) is not None
    assert rm.market_eligible(Market.from_api(make_market(hours_to_close=24 * 30))) is not None
    assert rm.market_eligible(Market.from_api(make_market(volume_24h=1))) is not None
    assert rm.market_eligible(Market.from_api(make_market(status="closed"))) is not None
    assert rm.market_eligible(Market.from_api(make_market())) is None


def test_sizing_respects_caps(settings):
    settings.max_position_dollars = 25.0
    settings.max_total_exposure_dollars = 200.0
    settings.min_position_dollars = 0.0
    rm = RiskManager(settings)
    # p=0.7 at 50c -> full kelly 0.4, quarter kelly 0.1 -> $100 of $1000, capped at $25 per market -> 50 contracts
    assert rm.size_position(0.7, 50, state()) == 50
    settings.max_contracts_per_order = 10
    assert rm.size_position(0.7, 50, state()) == 10
    settings.max_contracts_per_order = 100
    assert rm.size_position(0.7, 50, state(exposure_dollars=195.0)) == 10  # $5 room
    assert rm.size_position(0.7, 50, state(cash_dollars=0.0)) == 0


def test_size_capped_by_book_depth(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market())
    sig = rm.build_signal(m, book_for(m, depth=7), Estimate(0.80, 0.9, "manual"), state())
    assert sig.is_trade and sig.size == 7


def test_already_holding_passes(settings):
    rm = RiskManager(settings)
    m = Market.from_api(make_market())
    sig = rm.build_signal(m, book_for(m), Estimate(0.80, 0.9, "manual"), state(open_tickers={"KXTEST-1"}))
    assert sig.action == SignalAction.PASS and "already" in sig.rationale


def test_exit_rules(settings):
    rm = RiskManager(settings)
    assert rm.exit_reason("yes", 50, 60, 42, 41, None) == "stop-loss"
    assert rm.exit_reason("yes", 50, 60, 42, 42, None) == "stop-loss"
    assert rm.exit_reason("yes", 50, 60, 42, 55, None) is None
    assert rm.exit_reason("yes", 50, 60, 42, 60, None) == "take-profit"
    # still +EV vs the current bid -> hold to settlement
    assert rm.exit_reason("yes", 50, 60, 42, 60, 0.80) is None
    assert rm.exit_reason("yes", 50, 60, 42, 60, 0.62) == "take-profit"
    settings.hold_to_settlement_if_edge = False
    assert rm.exit_reason("yes", 50, 60, 42, 60, 0.80) == "take-profit"
    assert rm.exit_reason("yes", 50, 60, 42, None, None) is None


def test_position_floor_skips_sub_scale_trades(settings):
    """$3-$5 positions: better nothing than a position too small to be worth the fees."""
    settings.min_position_dollars = 3.0
    settings.max_position_dollars = 5.0
    settings.max_total_exposure_dollars = 25.0
    rm = RiskManager(settings)
    # plenty of room: capped at the $5 maximum
    size = rm.size_position(0.70, 50, state())
    assert size == 10 and size * 50 / 100 == 5.0
    # only $2 of exposure room left: skipped rather than nibbled
    assert rm.size_position(0.70, 50, state(exposure_dollars=23.0)) == 0
    # exactly $3 of room: taken
    assert rm.size_position(0.70, 50, state(exposure_dollars=22.0)) == 6
    # floor off: the $2 of room is used
    settings.min_position_dollars = 0.0
    assert rm.size_position(0.70, 50, state(exposure_dollars=23.0)) == 4


def test_exposure_cap_limits_concurrent_positions(settings):
    """$25 in play with $5 positions means five at a time, then no room."""
    settings.min_position_dollars = 3.0
    settings.max_position_dollars = 5.0
    settings.max_total_exposure_dollars = 25.0
    rm = RiskManager(settings)
    assert rm.size_position(0.70, 50, state(exposure_dollars=20.0)) > 0
    assert rm.size_position(0.70, 50, state(exposure_dollars=25.0)) == 0


def test_shipped_risk_defaults():
    """The caps a fresh install trades with, pinned so they cannot drift silently."""
    from kalshitrader.config import Settings

    s = Settings()
    assert (s.min_position_dollars, s.max_position_dollars) == (2.5, 5.0)
    assert s.max_total_exposure_dollars == 25.0
    assert s.daily_loss_limit_dollars == 50.0
    assert s.max_open_positions == 5
    assert s.bot_enabled is False and s.trading_mode == "paper"
