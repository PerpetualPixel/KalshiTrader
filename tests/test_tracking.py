import math

import pytest

from kalshitrader.tracking.db import Store
from kalshitrader.tracking.metrics import compute_metrics


def test_trade_lifecycle_and_pnl():
    st = Store(":memory:")
    tid = st.open_trade(mode="paper", ticker="T", title="t", side="yes", entry_price=50, count=10, take_profit=60, stop_loss=42,
                        p_true=0.7, fees=0.18, signal_id=None, close_time=None)
    assert st.open_trades()[0]["id"] == tid
    t = st.close_trade(tid, exit_price=60, exit_reason="take-profit", extra_fees=0.17)
    assert t["status"] == "closed"
    assert t["pnl"] == round((60 - 50) * 10 / 100 - 0.35, 10)
    assert st.open_trades() == []
    assert st.closed_trades()[0]["exit_reason"] == "take-profit"


def test_state_and_signals():
    st = Store(":memory:")
    assert st.get_state("halted", False) is False
    st.set_state("halted", True)
    assert st.get_state("halted") is True
    sid = st.add_signal({"created_at": "2026-01-01T00:00:00", "ticker": "T", "action": "BUY YES", "side": "yes", "entry_price": 50, "formatted": "x"})
    st.mark_signal_executed(sid)
    assert st.recent_signals()[0]["executed"] == 1
    assert st.signal_counts() == {"BUY YES": 1}
    assert st.recent_signals(trades_only=True)[0]["id"] == sid


def test_snapshots_latest_per_ticker():
    st = Store(":memory:")
    for bid in (40, 41, 42):
        st.add_snapshot({"ticker": "A", "yes_bid": bid, "yes_ask": bid + 2, "no_bid": 98 - bid, "no_ask": 60 - bid, "last_price": bid, "volume_24h": 10})
    st.add_snapshot({"ticker": "B", "yes_bid": 10, "yes_ask": 12, "no_bid": 88, "no_ask": 90, "last_price": 10, "volume_24h": 99})
    latest = st.latest_snapshots()
    assert [s["ticker"] for s in latest] == ["B", "A"]
    assert next(s for s in latest if s["ticker"] == "A")["yes_bid"] == 42
    assert [s["yes_bid"] for s in st.snapshots("A")] == [40, 41, 42]


def test_metrics_basic():
    trades = [
        {"status": "closed", "pnl": 10.0, "closed_at": "2026-01-01T10:00:00", "fees": 0.5},
        {"status": "closed", "pnl": -4.0, "closed_at": "2026-01-02T10:00:00", "fees": 0.5},
        {"status": "closed", "pnl": -2.0, "closed_at": "2026-01-03T10:00:00", "fees": 0.5},
        {"status": "open", "pnl": None, "fees": 0.2},
    ]
    equity = [
        {"equity": 1000, "cash": 1000, "unrealized_pnl": 0},
        {"equity": 1010, "cash": 1010, "unrealized_pnl": 0},
        {"equity": 1004, "cash": 1004, "unrealized_pnl": 0},
        {"equity": 1002, "cash": 992, "unrealized_pnl": 1.0},
    ]
    m = compute_metrics(trades, equity, 1000)
    assert m["trades_closed"] == 3 and m["trades_open"] == 1
    assert m["wins"] == 1 and m["losses"] == 2
    assert m["win_rate"] == 1 / 3
    assert m["profit_factor"] == 10 / 6
    assert m["realized_pnl"] == 4.0
    assert m["consecutive_losses"] == 2
    assert m["max_drawdown"] == 8 and m["max_drawdown_pct"] == pytest.approx(8 / 1010)
    assert m["equity"] == 1002 and m["unrealized_pnl"] == 1.0
    assert m["total_fees"] == pytest.approx(1.7)
    assert m["total_return"] == pytest.approx(0.002)


def test_metrics_empty():
    m = compute_metrics([], [], 500)
    assert m["win_rate"] == 0.0 and m["profit_factor"] == 0.0 and m["equity"] == 500
    assert not math.isnan(m["sharpe"])
