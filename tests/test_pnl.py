from src.pnl import compute_pnl


def fill(time, ticker, side, action, count, price, fee=0):
    return {
        "time": time,
        "ticker": ticker,
        "side": side,
        "action": action,
        "count": count,
        "price_cents": price,
        "raw": {"fee": fee} if fee else {},
    }


def test_round_trip_sell_profit():
    fills = [
        fill("2026-01-01T10:00:00", "MKT-A", "yes", "buy", 10, 40),
        fill("2026-01-01T11:00:00", "MKT-A", "yes", "sell", 10, 55),
    ]
    result = compute_pnl(fills, [])
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["net_cents"] == 10 * 55 - 10 * 40  # +150c
    assert trade["kind"] == "sell"
    assert result["total_net_cents"] == 150
    assert result["by_ticker"]["MKT-A"] == 150
    assert result["open_positions"] == []


def test_partial_sell_uses_average_cost():
    fills = [
        fill("t1", "MKT-A", "yes", "buy", 10, 40),
        fill("t2", "MKT-A", "yes", "buy", 10, 60),  # avg cost now 50
        fill("t3", "MKT-A", "yes", "sell", 10, 55),
    ]
    result = compute_pnl(fills, [])
    assert result["trades"][0]["net_cents"] == 10 * 55 - 10 * 50  # +50c
    assert len(result["open_positions"]) == 1
    assert result["open_positions"][0]["count"] == 10


def test_settlement_win_and_loss():
    fills = [
        fill("t1", "MKT-A", "yes", "buy", 5, 40),
        fill("t2", "MKT-A", "no", "buy", 5, 50),
    ]
    settlements = [{"time": "t3", "ticker": "MKT-A", "raw": {"market_result": "yes"}}]
    result = compute_pnl(fills, settlements)
    by_kind = {(t["side"]): t for t in result["trades"]}
    assert by_kind["yes"]["net_cents"] == 5 * 100 - 5 * 40  # +300c winner
    assert by_kind["no"]["net_cents"] == 0 - 5 * 50  # -250c loser
    assert result["total_net_cents"] == 50


def test_fees_reduce_profit():
    fills = [
        fill("t1", "MKT-A", "yes", "buy", 10, 40, fee=7),
        fill("t2", "MKT-A", "yes", "sell", 10, 55, fee=7),
    ]
    result = compute_pnl(fills, [])
    # buy fee raises cost basis by 7, sell fee subtracted directly
    assert result["total_net_cents"] == 150 - 14


def test_cumulative_curve_is_running_total():
    fills = [
        fill("t1", "A", "yes", "buy", 1, 40),
        fill("t2", "A", "yes", "sell", 1, 50),  # +10
        fill("t3", "B", "yes", "buy", 1, 40),
        fill("t4", "B", "yes", "sell", 1, 30),  # -10
    ]
    result = compute_pnl(fills, [])
    assert [p["net_cents"] for p in result["cumulative"]] == [10, 0]


def test_sell_without_basis_is_ignored():
    fills = [fill("t1", "MKT-A", "yes", "sell", 5, 50)]
    result = compute_pnl(fills, [])
    assert result["trades"] == []
    assert result["total_net_cents"] == 0
