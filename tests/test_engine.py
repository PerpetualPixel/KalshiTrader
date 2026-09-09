"""End-to-end paper-trading cycles against the fake exchange."""
from kalshitrader.analysis.estimators import ManualEstimator
from kalshitrader.execution.paper import PaperBroker
from kalshitrader.strategy.engine import Engine
from kalshitrader.tracking.db import Store
from tests.conftest import make_market


def make_engine(settings, client, store):
    broker = PaperBroker(store, settings.paper_starting_cash, settings.paper_slippage_cents, settings.fee_rate)
    return Engine(settings, client, store, broker)


def test_paper_round_trip_take_profit(settings, client, exchange, store):
    ManualEstimator(settings.manual_estimates_path).set("KXTEST-1", 0.75, 0.9, "strong research")
    engine = make_engine(settings, client, store)

    res = engine.cycle()
    assert res.error is None
    assert res.trades_opened == 1
    trade = store.open_trades()[0]
    assert trade["side"] == "yes" and trade["entry_price"] == 50
    assert trade["take_profit"] == 60 and trade["stop_loss"] == 42
    assert engine.broker.cash_dollars() < settings.paper_starting_cash
    assert store.recent_signals(trades_only=True)[0]["executed"] == 1
    assert store.latest_equity()["open_positions"] == 1

    # same market again -> PASS (already holding), no second entry
    res = engine.cycle()
    assert res.trades_opened == 0 and len(store.open_trades()) == 1

    # blended p_true (~0.68: manual 0.75 damped by the book) vs bid 60 -> net EV ~6c > min edge -> hold
    assert 0.6 < trade["p_true"] < 0.75
    exchange.set_price("KXTEST-1", 60, 62)
    assert engine.cycle().trades_closed == 0

    # bid at 68: EV vs bid drops under threshold -> take profit
    exchange.set_price("KXTEST-1", 68, 70)
    res = engine.cycle()
    assert res.trades_closed == 1
    closed = store.closed_trades()[0]
    assert closed["exit_reason"] == "take-profit" and closed["exit_price"] == 68
    assert closed["pnl"] > 0
    assert engine.broker.cash_dollars() > settings.paper_starting_cash


def test_paper_stop_loss(settings, client, exchange, store):
    ManualEstimator(settings.manual_estimates_path).set("KXTEST-1", 0.75, 0.9)
    engine = make_engine(settings, client, store)
    engine.cycle()
    exchange.set_price("KXTEST-1", 41, 43)
    res = engine.cycle()
    assert res.trades_closed == 1
    closed = store.closed_trades()[0]
    assert closed["exit_reason"] == "stop-loss" and closed["pnl"] < 0


def test_paper_settlement(settings, client, exchange, store):
    ManualEstimator(settings.manual_estimates_path).set("KXTEST-1", 0.75, 0.9)
    engine = make_engine(settings, client, store)
    engine.cycle()
    count = store.open_trades()[0]["count"]
    cash_before = engine.broker.cash_dollars()
    exchange.settle("KXTEST-1", "yes")
    res = engine.cycle()
    assert res.trades_closed == 1
    t = store.closed_trades()[0]
    assert t["status"] == "settled" and t["exit_price"] == 100
    assert engine.broker.cash_dollars() == round(cash_before + count, 4)


def test_settlement_loss(settings, client, exchange, store):
    ManualEstimator(settings.manual_estimates_path).set("KXTEST-1", 0.75, 0.9)
    engine = make_engine(settings, client, store)
    engine.cycle()
    cash_before = engine.broker.cash_dollars()
    exchange.settle("KXTEST-1", "no")
    engine.cycle()
    t = store.closed_trades()[0]
    assert t["exit_price"] == 0 and t["pnl"] < 0
    assert engine.broker.cash_dollars() == cash_before


def test_halt_blocks_entries_but_not_exits(settings, client, exchange, store):
    ManualEstimator(settings.manual_estimates_path).set("KXTEST-1", 0.75, 0.9)
    engine = make_engine(settings, client, store)
    engine.cycle()
    store.set_state("halted", True)
    exchange.add(make_market("KXTEST-2", yes_bid=48, yes_ask=50))
    ManualEstimator(settings.manual_estimates_path).set("KXTEST-2", 0.80, 0.9)
    exchange.set_price("KXTEST-1", 40, 42)
    res = engine.cycle()
    assert res.trades_opened == 0 and res.trades_closed == 1


def test_scan_without_execute_records_signals_only(settings, client, exchange, store):
    ManualEstimator(settings.manual_estimates_path).set("KXTEST-1", 0.75, 0.9)
    engine = make_engine(settings, client, store)
    res = engine.scan(execute=False)
    assert len(res.trade_signals) == 1 and res.trades_opened == 0
    assert store.open_trades() == []
    assert store.recent_signals(trades_only=True)[0]["executed"] == 0


def test_microstructure_alone_passes(settings, client, exchange, store):
    """With only the order-book estimator, the bot should sit on its hands."""
    settings.estimators = ["microstructure"]
    engine = make_engine(settings, client, store)
    res = engine.cycle()
    assert res.trades_opened == 0
    assert all(not s.is_trade for s in res.signals)


def test_cycle_survives_exchange_outage(settings, client, exchange, store):
    engine = make_engine(settings, client, store)
    exchange.fail_next = [500] * 6
    res = engine.cycle()
    assert res.error and "500" in res.error
    assert store.recent_runs()[0]["error"]


def test_series_filter(settings, client, exchange, store):
    exchange.add({**make_market("OTHER-1"), "series_ticker": "OTHER"})
    settings.series_tickers = ["KXTEST"]
    engine = make_engine(settings, client, store)
    assert [m.ticker for m in engine.fetch_markets()] == ["KXTEST-1"]


def test_paper_broker_rejects_when_broke(tmp_path):
    st = Store(":memory:")
    b = PaperBroker(st, starting_cash=0.10, slippage_cents=0)
    assert b.buy("T", "yes", 50, 10).status == "rejected"
    b2 = PaperBroker(Store(":memory:"), starting_cash=2.0, slippage_cents=0)
    fill = b2.buy("T", "yes", 50, 10)  # can afford ~3 contracts + fee
    assert 0 < fill.count < 10
