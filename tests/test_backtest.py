"""The backtester replays recorded prices through the real strategy.

The value of it rests entirely on it being the same rules the live loop runs, so the
first test pins that: same history, same settings, same trades.
"""
from datetime import datetime, timedelta, timezone

import pytest

from kalshitrader.backtest import load_history, run_backtest, sweep_settings
from kalshitrader.config import Settings
from kalshitrader.tracking.db import Store

START = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)


def record(store: Store, ticker: str, prices: list[int], *, every_seconds: int = 20, volume_step: int = 5) -> None:
    """Write snapshots the way the live loop does."""
    for i, bid in enumerate(prices):
        store._conn.execute(
            "INSERT INTO market_snapshots(ts, ticker, yes_bid, yes_ask, no_bid, no_ask, last_price,"
            " volume_24h, open_interest, volume) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ((START + timedelta(seconds=every_seconds * i)).isoformat(), ticker, bid, bid + 1,
             99 - bid, 100 - bid, bid, 5000, 900, 100 + i * volume_step))
    store._conn.commit()


def settings(tmp_path, **over) -> Settings:
    base = dict(db_path=str(tmp_path / "b.db"), min_depth_contracts=0, min_volume_24h=0,
                paper_starting_cash=1000.0, paper_slippage_cents=1, bot_enabled=True,
                dip_cents=14, take_profit_cents=10, stop_loss_cents=8, min_edge_cents=3.0)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "b.db"))


# ------------------------------------------------------------------ the basics
def test_a_dip_that_recovers_is_bought_and_sold_at_the_target(store, tmp_path):
    # up to 70, down to 50 (a 20c dip), then back through the target
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55, 60, 66, 72, 78])
    r = run_backtest(store, settings(tmp_path))
    assert r.closed, "a 20c dip that recovered should have traded"
    t = r.closed[0]
    assert t["exit_reason"] == "take-profit"
    assert t["pnl"] > 0
    assert t["fees"] > 0, "both fees are charged, as on the exchange"


def test_a_dip_that_keeps_falling_is_stopped_out(store, tmp_path):
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 48, 44, 38, 30, 24, 20])
    r = run_backtest(store, settings(tmp_path))
    assert r.closed and r.closed[0]["exit_reason"] == "stop-loss"
    assert r.closed[0]["pnl"] < 0


def test_no_stop_means_a_falling_position_is_still_held(store, tmp_path):
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 48, 44, 38, 30, 24, 20])
    r = run_backtest(store, settings(tmp_path, use_stop_loss=False))
    assert not r.closed, "with no stop it should ride, not close"
    assert r.open_at_end == 1


def test_flat_prices_never_trade(store, tmp_path):
    record(store, "KX-A", [50] * 40)
    assert run_backtest(store, settings(tmp_path)).trades == []


# ------------------------------------------------- agreement with the live loop
def test_the_backtest_and_the_live_engine_reach_the_same_verdict(store, tmp_path):
    """A backtester that has drifted from the strategy is worse than none: it would
    answer questions about code nobody runs. Same prices, same settings, same call."""
    from kalshitrader.backtest.replay import ReplayStore, _market_from
    from kalshitrader.markets.contest import NO_SIDE, Contest, Leg
    from kalshitrader.risk.rules import PortfolioState, RiskManager
    from kalshitrader.trading.strategy import SwingStrategy

    prices = [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55]
    record(store, "KX-A", prices)
    s = settings(tmp_path)
    bt = run_backtest(store, s)

    # Now put the same history in front of the strategy directly, at the same instant.
    history = load_history(store)
    replay = ReplayStore(history)
    opened = bt.trades[0]
    replay.now = opened["opened_at"]
    strategy = SwingStrategy(s, replay, RiskManager(s))
    snap = replay.snapshots("KX-A", limit=1)[-1]
    market = _market_from(snap, "KX-A")
    legs = {"KX-A": Leg("KX-A", "KX-A", "yes", market), NO_SIDE: Leg(NO_SIDE, "KX-A", "no", market)}
    contest = Contest("KX-A", "KX-A", NO_SIDE, legs, "KX", market.close_time, [market], "KX")
    state = PortfolioState(cash_dollars=s.paper_starting_cash, exposure_dollars=0.0, open_positions=0,
                           daily_pnl_dollars=0.0, consecutive_losses=0, open_tickers=set(), halted=False)
    signal = strategy.evaluate_leg(contest, "KX-A", legs["KX-A"], None, None, state,
                                   datetime.fromisoformat(opened["opened_at"]))
    assert signal.is_trade, signal.rationale
    assert signal.entry_price + s.paper_slippage_cents == opened["entry_price"]
    assert signal.take_profit == opened["take_profit"] and signal.stop_loss == opened["stop_loss"]


# ----------------------------------------------------------------- the numbers
def test_reported_pnl_is_after_both_fees(store, tmp_path):
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55, 60, 66, 72, 78])
    t = run_backtest(store, settings(tmp_path)).closed[0]
    gross = (t["exit_price"] - t["entry_price"]) * t["count"] / 100
    assert t["pnl"] == pytest.approx(gross - t["fees"]), "P&L must be net, like the live ledger"


def test_break_even_win_rate_comes_from_what_the_trades_returned(store, tmp_path):
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55, 60, 66, 72, 78])
    record(store, "KX-B", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 48, 44, 38, 30, 24, 20])
    r = run_backtest(store, settings(tmp_path, reentry_cooldown_minutes=0))
    if r.wins and r.losses:
        avg_win = sum(t["pnl"] for t in r.wins) / len(r.wins)
        avg_loss = -sum(t["pnl"] for t in r.losses) / len(r.losses)
        assert r.break_even_win_rate == pytest.approx(avg_loss / (avg_win + avg_loss))
    assert run_backtest(store, settings(tmp_path, dip_cents=99)).break_even_win_rate is None


def test_exposure_and_position_caps_are_honoured(store, tmp_path):
    """The replay must respect the same limits, or it reports profits from a size the
    bot would never have taken."""
    for i in range(6):
        record(store, f"KX-{i}", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55])
    r = run_backtest(store, settings(tmp_path, max_open_positions=2, reentry_cooldown_minutes=0))
    by_time: dict[str, int] = {}
    for t in r.trades:
        by_time[t["opened_at"]] = by_time.get(t["opened_at"], 0) + 1
    assert len(r.trades) <= 2, f"opened {len(r.trades)} against a cap of 2"


def test_the_cooldown_stops_one_decline_being_replayed_as_many_trades(store, tmp_path):
    record(store, "KX-A", [70] * 5 + [64, 58, 52, 46, 40, 34, 28, 22, 16, 10, 8, 6])
    assert len(run_backtest(store, settings(tmp_path)).trades) == 1


# ------------------------------------------------------------------- mechanics
def test_a_sweep_compares_configurations_over_one_shared_history(store, tmp_path):
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55, 60, 66, 72, 78])
    results = sweep_settings(store, settings(tmp_path), [{"dip_cents": d} for d in (8, 14, 40)])
    assert len(results) == 3
    assert [r.snapshots for r in results] == [r.snapshots for r in results][:1] * 3, "same span for each"
    assert results[-1].trades == [], "a 40c dip never happens in this history"


def test_an_empty_database_reports_nothing_rather_than_failing(store, tmp_path):
    r = run_backtest(store, settings(tmp_path))
    assert r.trades == [] and r.tickers == 0 and r.summary()["trades"] == 0


def test_history_can_be_limited_by_time_and_ticker(store, tmp_path):
    record(store, "KX-A", [50] * 10)
    record(store, "KX-B", [50] * 10)
    assert set(load_history(store)) == {"KX-A", "KX-B"}
    assert set(load_history(store, tickers=["KX-A"])) == {"KX-A"}
    later = (START + timedelta(seconds=100)).isoformat()
    assert all(r["ts"] >= later for rows in load_history(store, since=later).values() for r in rows)


# ------------------------------------------------------- maker vs taker fills
def test_a_maker_order_buys_at_the_bid_not_the_ask(store, tmp_path):
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55, 60, 66, 72, 78])
    taker = run_backtest(store, settings(tmp_path))
    maker = run_backtest(store, settings(tmp_path), maker=True)
    assert taker.closed and maker.closed
    # The recorded book is bid/bid+1, and the taker also pays a cent of slippage.
    assert maker.closed[0]["entry_price"] < taker.closed[0]["entry_price"]
    assert maker.closed[0]["exit_price"] > taker.closed[0]["exit_price"], "a resting sell rests at the ask"
    assert maker.pnl > taker.pnl, "not crossing the spread on either side has to be worth something"


def test_a_maker_order_does_not_fill_when_nothing_trades(store, tmp_path):
    """Posting is not buying. If the volume counter never moves, nobody traded against
    the order and it must expire rather than being handed a free fill.

    Liveness is taken off the gate here so the test is about the fill model alone -
    a market with no volume would otherwise never be judged live enough to buy.
    """
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55, 60, 66], volume_step=0)
    s = settings(tmp_path, live_only=False)
    r = run_backtest(store, s, maker=True, maker_ttl_seconds=60)
    assert r.posted >= 1, "the strategy should still have wanted to buy"
    assert r.trades == [], "no counterparty means no position"
    assert r.unfilled == r.posted and r.fill_rate == 0.0
    # The same history, crossing the spread, does trade: only the fill model differs.
    assert run_backtest(store, s).trades, "the taker path should still have entered"


def test_a_maker_order_expires_instead_of_resting_into_a_moved_market(store, tmp_path):
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50] + [50] * 60, volume_step=0)
    r = run_backtest(store, settings(tmp_path, live_only=False), maker=True, maker_ttl_seconds=40)
    assert r.unfilled >= 1, "an order nobody traded against must be given up on"
    assert r.posted > r.unfilled or r.trades == []


def test_the_fill_rate_is_reported_so_maker_results_can_be_discounted(store, tmp_path):
    record(store, "KX-A", [60] * 5 + [70] * 5 + [64, 58, 52, 50, 51, 55, 60, 66, 72, 78])
    assert run_backtest(store, settings(tmp_path)).fill_rate is None, "taker always fills; no rate to report"
    r = run_backtest(store, settings(tmp_path), maker=True)
    assert r.fill_rate is not None and 0.0 <= r.fill_rate <= 1.0


# ------------------------------------------------- why a replay bought nothing
def test_a_replay_that_buys_nothing_says_which_gate_turned_it_away(store, tmp_path):
    # A 6c wobble against a 14c dip requirement: the dip gate should own every check.
    record(store, "KX-A", [60, 62, 64, 58, 60, 62, 64, 58, 60, 62])
    r = run_backtest(store, settings(tmp_path))
    assert not r.trades
    assert r.evaluations > 0
    top = r.why_nothing[0]
    assert "dip" in top["reason"]
    checks = sum(row["count"] for row in r.why_nothing)
    assert checks == r.evaluations, "every check that passed is accounted for"
    assert top["count"] > checks / 2, "the dip gate, not something incidental, did the blocking"
    assert top["ticker"] == "KX-A"
    # Numbers are blanked so thousands of near-identical rationales collapse to one row.
    assert "#" in top["reason"] and top["example"] != top["reason"]


def test_a_dead_tape_is_reported_as_the_liveness_gate_not_as_a_strategy_result(store, tmp_path):
    # Prices that swing plenty, but the cumulative volume counter never moves.
    record(store, "KX-A", [60] * 5 + [70] * 5 + [50, 52, 56, 60, 66], volume_step=0)
    r = run_backtest(store, settings(tmp_path, live_only=True, live_min_volume_delta=1))
    assert not r.trades
    assert "not live" in r.why_nothing[0]["reason"]
    # Same history, same dip, liveness off: the prices were always tradeable, only the
    # tape was missing. This is what `backtest --ignore-liveness` turns on.
    assert run_backtest(store, settings(tmp_path, live_only=False)).trades


def test_the_data_profile_measures_dips_without_applying_any_gates(store, tmp_path):
    from kalshitrader.backtest import data_profile

    record(store, "KX-DEEP", [60] * 5 + [80] * 5 + [55, 56, 58])  # 25c below its high
    record(store, "KX-FLAT", [40] * 12, volume_step=0)
    p = data_profile(load_history(store), window_minutes=45)

    assert p.tickers == 2
    assert p.quoted_tickers == 2
    assert p.traded_tickers == 1, "KX-DEEP's volume counter moved; KX-FLAT's did not"
    assert p.max_dip == 25
    assert dict(p.deepest)["KX-FLAT"] == 0
    assert p.dips_at_least[20] == 1 and p.dips_at_least[2] == 1


def test_the_data_profile_only_looks_back_over_the_swing_window(store, tmp_path):
    from kalshitrader.backtest import data_profile

    # An 81c ask, twenty minutes at 61c, then 56c. Over 45m the last price is measured
    # against the 81c high for a 25c dip. Over 5m that high has aged out long before the
    # fall, so the deepest dip the window ever sees is the original 81c -> 61c drop.
    record(store, "KX-A", [80] * 3 + [60] * 60 + [55], every_seconds=20)
    assert data_profile(load_history(store), window_minutes=45).max_dip == 25
    assert data_profile(load_history(store), window_minutes=5).max_dip == 20
