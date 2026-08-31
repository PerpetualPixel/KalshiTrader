"""Trade-level PnL from the local fill/settlement ledger.

Uses average-cost accounting per (ticker, side):
  - buys increase the open lot's size and cost basis
  - sells realize (sale proceeds - average cost) on the sold contracts
  - settlements realize (payout - average cost) on the remaining contracts,
    where payout is 100c/contract for the winning side, 0c for the losing side

Each realizing event becomes a "trade" row with its own timestamp and net
profit, which also feeds the cumulative net-profit curve and the per-ticker
PnL chart. Fees reported by the API on fills/settlements are subtracted when
present.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _fee_cents(raw: dict[str, Any]) -> int:
    for key in ("fee", "fee_cents", "taker_fee_cents", "taker_fee"):
        value = raw.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def compute_pnl(
    fills: list[dict[str, Any]], settlements: list[dict[str, Any]]
) -> dict[str, Any]:
    """`fills` and `settlements` must be in ascending time order.

    Fill rows need: time, ticker, side, action, count, price_cents, raw.
    Settlement rows need: time, ticker, raw (Kalshi settlement payload).
    """
    # (ticker, side) -> [count, total_cost_cents]
    lots: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    trades: list[dict[str, Any]] = []

    events: list[tuple[str, str, dict[str, Any]]] = [
        (f["time"], "fill", f) for f in fills
    ] + [(s["time"], "settlement", s) for s in settlements]
    events.sort(key=lambda e: e[0])

    def realize(
        time_: str, ticker: str, side: str, count: int,
        proceeds_cents: int, cost_cents: int, fee_cents: int, kind: str,
    ) -> None:
        trades.append(
            {
                "time": time_,
                "ticker": ticker,
                "side": side,
                "count": count,
                "proceeds_cents": proceeds_cents,
                "cost_cents": cost_cents,
                "fee_cents": fee_cents,
                "net_cents": proceeds_cents - cost_cents - fee_cents,
                "kind": kind,
            }
        )

    for time_, kind, ev in events:
        if kind == "fill":
            key = (ev["ticker"], ev["side"])
            count = int(ev["count"])
            price = int(ev["price_cents"])
            fee = _fee_cents(ev.get("raw") or {})
            lot = lots[key]
            if ev["action"] == "buy":
                lot[0] += count
                lot[1] += count * price + fee
            else:  # sell
                sold = min(count, lot[0])
                if sold <= 0:
                    continue  # sale with no tracked basis (pre-existing position)
                avg_cost = lot[1] / lot[0]
                cost = round(avg_cost * sold)
                lot[0] -= sold
                lot[1] -= cost
                realize(time_, ev["ticker"], ev["side"], sold, sold * price, cost, fee, "sell")
        else:  # settlement: pays 100c per winning contract, 0c per losing
            raw = ev.get("raw") or {}
            ticker = ev["ticker"]
            result = str(raw.get("market_result", "")).lower()  # "yes" | "no"
            for side in ("yes", "no"):
                lot = lots[(ticker, side)]
                if lot[0] <= 0:
                    continue
                count, cost = lot[0], lot[1]
                payout = count * 100 if side == result else 0
                lots[(ticker, side)] = [0, 0]
                realize(time_, ticker, side, count, payout, cost, 0, "settlement")

    cumulative: list[dict[str, Any]] = []
    running = 0
    for t in trades:
        running += t["net_cents"]
        cumulative.append({"time": t["time"], "net_cents": running})

    by_ticker: dict[str, int] = defaultdict(int)
    for t in trades:
        by_ticker[t["ticker"]] += t["net_cents"]

    open_positions = [
        {
            "ticker": ticker,
            "side": side,
            "count": lot[0],
            "cost_cents": lot[1],
            "avg_price_cents": round(lot[1] / lot[0], 1) if lot[0] else 0,
        }
        for (ticker, side), lot in lots.items()
        if lot[0] > 0
    ]

    return {
        "trades": list(reversed(trades)),  # newest first for the table
        "cumulative": cumulative,
        "by_ticker": dict(sorted(by_ticker.items(), key=lambda kv: kv[1])),
        "total_net_cents": running,
        "open_positions": open_positions,
    }
