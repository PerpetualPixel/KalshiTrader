"""Performance metrics computed from the trade and equity tables."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def _day(ts: str) -> str:
    return ts[:10]


def compute_metrics(trades: list[dict], equity: list[dict], starting_cash: float | None = None) -> dict:
    closed = [t for t in trades if t.get("status") != "open" and t.get("pnl") is not None]
    open_ = [t for t in trades if t.get("status") == "open"]
    pnls = [float(t["pnl"]) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)

    # consecutive losses, most recent first
    consecutive_losses = 0
    for t in sorted(closed, key=lambda t: t.get("closed_at") or "", reverse=True):
        if float(t["pnl"]) <= 0:
            consecutive_losses += 1
        else:
            break

    today = datetime.now(timezone.utc).date().isoformat()
    daily_pnl = sum(float(t["pnl"]) for t in closed if _day(t.get("closed_at") or "") == today)

    # drawdown from equity curve
    peak = -math.inf
    max_dd = 0.0
    max_dd_pct = 0.0
    for row in equity:
        e = float(row["equity"])
        peak = max(peak, e)
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak if peak > 0 else 0.0

    # daily returns -> annualised Sharpe (0 when < 2 days)
    by_day: dict[str, float] = defaultdict(float)
    for t in closed:
        by_day[_day(t.get("closed_at") or "")] += float(t["pnl"])
    base = starting_cash or (float(equity[0]["equity"]) if equity else 0.0) or 1.0
    daily_returns = [v / base for v in by_day.values()]
    sharpe = 0.0
    if len(daily_returns) >= 2:
        mean = sum(daily_returns) / len(daily_returns)
        var = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        sd = math.sqrt(var)
        if sd > 0:
            sharpe = mean / sd * math.sqrt(365)

    latest = equity[-1] if equity else None
    first = equity[0] if equity else None
    total_return = 0.0
    if latest and first and float(first["equity"]) > 0:
        total_return = float(latest["equity"]) / float(first["equity"]) - 1

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week_pnl = sum(float(t["pnl"]) for t in closed if (t.get("closed_at") or "") >= week_ago)

    return {
        "trades_closed": len(closed),
        "trades_open": len(open_),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        "expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
        "realized_pnl": sum(pnls),
        "unrealized_pnl": float(latest["unrealized_pnl"]) if latest else 0.0,
        "daily_pnl": daily_pnl,
        "week_pnl": week_pnl,
        "consecutive_losses": consecutive_losses,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "sharpe": sharpe,
        "total_return": total_return,
        "equity": float(latest["equity"]) if latest else (starting_cash or 0.0),
        "cash": float(latest["cash"]) if latest else (starting_cash or 0.0),
        "total_fees": sum(float(t.get("fees") or 0) for t in trades),
    }
