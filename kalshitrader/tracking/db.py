"""SQLite persistence for signals, orders, trades, equity and market snapshots.

One file, WAL mode, safe for the bot process and the dashboard process to
share. All money columns are dollars (REAL); all prices are cents (INTEGER).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    title TEXT,
    action TEXT NOT NULL,
    side TEXT,
    entry_price INTEGER,
    p_true REAL,
    p_market REAL,
    confidence REAL,
    ev_gross REAL,
    ev_net REAL,
    spread INTEGER,
    depth INTEGER,
    take_profit INTEGER,
    stop_loss INTEGER,
    size INTEGER,
    flags TEXT,
    estimator TEXT,
    rationale TEXT,
    close_time TEXT,
    formatted TEXT,
    executed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    count INTEGER NOT NULL,
    filled INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    trade_id INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ticker TEXT NOT NULL,
    title TEXT,
    side TEXT NOT NULL,
    entry_price INTEGER NOT NULL,
    count INTEGER NOT NULL,
    take_profit INTEGER,
    stop_loss INTEGER,
    p_true REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_price INTEGER,
    exit_reason TEXT,
    fees REAL NOT NULL DEFAULT 0,
    pnl REAL,
    status TEXT NOT NULL DEFAULT 'open',
    signal_id INTEGER,
    close_time TEXT,
    close_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    equity REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    open_positions INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    yes_bid INTEGER, yes_ask INTEGER, no_bid INTEGER, no_ask INTEGER,
    last_price INTEGER, volume_24h INTEGER, open_interest INTEGER, volume INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_ticker_ts ON market_snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    markets_scanned INTEGER NOT NULL,
    signals INTEGER NOT NULL,
    trades_opened INTEGER NOT NULL,
    trades_closed INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS assessments (
    match_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r["name"] for r in self._q("PRAGMA table_info(market_snapshots)")}
        if "volume" not in cols:
            with self._lock:
                self._conn.execute("ALTER TABLE market_snapshots ADD COLUMN volume INTEGER")
        cols = {r["name"] for r in self._q("PRAGMA table_info(trades)")}
        if "close_requested" not in cols:
            with self._lock:
                self._conn.execute("ALTER TABLE trades ADD COLUMN close_requested INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _q(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self._lock:
            return _row(self._conn.execute(sql, params).fetchone())

    # ---------------------------------------------------------------- state
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), utcnow()),
            )

    # ---------------------------------------------------------- assessments
    def get_assessment(self, match_key: str) -> dict | None:
        row = self._one("SELECT payload FROM assessments WHERE match_key=? AND expires_at > ?", (match_key, utcnow()))
        return json.loads(row["payload"]) if row else None

    def put_assessment(self, match_key: str, payload: dict, ttl_minutes: int) -> None:
        from datetime import timedelta

        expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO assessments(match_key, created_at, expires_at, payload) VALUES(?,?,?,?) "
                "ON CONFLICT(match_key) DO UPDATE SET created_at=excluded.created_at, expires_at=excluded.expires_at, payload=excluded.payload",
                (match_key, utcnow(), expires, json.dumps(payload)),
            )

    def all_assessments(self) -> dict[str, dict]:
        rows = self._q("SELECT match_key, payload FROM assessments WHERE expires_at > ?", (utcnow(),))
        return {r["match_key"]: json.loads(r["payload"]) for r in rows}

    # -------------------------------------------------------------- signals
    def add_signal(self, rec: dict, executed: bool = False) -> int:
        cols = [
            "created_at", "ticker", "title", "action", "side", "entry_price", "p_true", "p_market", "confidence",
            "ev_gross", "ev_net", "spread", "depth", "take_profit", "stop_loss", "size", "flags", "estimator",
            "rationale", "close_time", "formatted",
        ]
        vals = [rec.get(c) for c in cols] + [1 if executed else 0]
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO signals({', '.join(cols)}, executed) VALUES({', '.join('?' * (len(cols) + 1))})", vals
            )
            return int(cur.lastrowid)

    def mark_signal_executed(self, signal_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE signals SET executed=1 WHERE id=?", (signal_id,))

    def recent_signals(self, limit: int = 50, trades_only: bool = False) -> list[dict]:
        where = "WHERE action IN ('BUY YES','BUY NO')" if trades_only else ""
        return self._q(f"SELECT * FROM signals {where} ORDER BY id DESC LIMIT ?", (limit,))

    def signal_counts(self, since: str | None = None) -> dict:
        where = "WHERE created_at >= ?" if since else ""
        params = (since,) if since else ()
        rows = self._q(f"SELECT action, COUNT(*) AS n FROM signals {where} GROUP BY action", params)
        return {r["action"]: r["n"] for r in rows}

    # --------------------------------------------------------------- orders
    def add_order(self, *, mode: str, ticker: str, action: str, side: str, price: int, count: int, status: str,
                  filled: int = 0, broker_order_id: str | None = None, trade_id: int | None = None, note: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO orders(created_at, mode, ticker, action, side, price, count, filled, status, broker_order_id, trade_id, note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), mode, ticker, action, side, price, count, filled, status, broker_order_id, trade_id, note),
            )
            return int(cur.lastrowid)

    def update_order(self, order_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE orders SET {sets} WHERE id=?", (*fields.values(), order_id))

    def recent_orders(self, limit: int = 50) -> list[dict]:
        return self._q("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    def pending_orders(self) -> list[dict]:
        return self._q("SELECT * FROM orders WHERE status IN ('resting','pending') ORDER BY id")

    # --------------------------------------------------------------- trades
    def open_trade(self, *, mode: str, ticker: str, title: str, side: str, entry_price: int, count: int,
                   take_profit: int, stop_loss: int, p_true: float | None, fees: float, signal_id: int | None,
                   close_time: str | None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades(mode, ticker, title, side, entry_price, count, take_profit, stop_loss, p_true, opened_at, fees, status, signal_id, close_time)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                (mode, ticker, title, side, entry_price, count, take_profit, stop_loss, p_true, utcnow(), fees, signal_id, close_time),
            )
            return int(cur.lastrowid)

    def request_close(self, trade_id: int) -> bool:
        """Ask the trading loop to sell this position on its next cycle.

        The dashboard runs in its own process and holds no broker, so it cannot
        place the order itself - and should not, or two processes could sell the
        same position twice. It sets a flag; the loop, which owns execution, acts
        on it. Returns False when the trade is not open (already closed, or gone).
        """
        with self._lock, self._conn as c:
            cur = c.execute("UPDATE trades SET close_requested = 1 WHERE id = ? AND status = 'open'", (trade_id,))
            return cur.rowcount > 0

    def clear_close_request(self, trade_id: int) -> None:
        with self._lock, self._conn as c:
            c.execute("UPDATE trades SET close_requested = 0 WHERE id = ?", (trade_id,))

    def close_trade(self, trade_id: int, *, exit_price: int, exit_reason: str, extra_fees: float = 0.0, status: str = "closed") -> dict:
        t = self.get_trade(trade_id)
        if t is None:
            raise KeyError(trade_id)
        fees = float(t["fees"]) + extra_fees
        pnl = (exit_price - t["entry_price"]) * t["count"] / 100.0 - fees
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET closed_at=?, exit_price=?, exit_reason=?, fees=?, pnl=?, status=? WHERE id=?",
                (utcnow(), exit_price, exit_reason, fees, pnl, status, trade_id),
            )
        return self.get_trade(trade_id)  # type: ignore[return-value]

    def get_trade(self, trade_id: int) -> dict | None:
        return self._one("SELECT * FROM trades WHERE id=?", (trade_id,))

    def open_trades(self, mode: str | None = None) -> list[dict]:
        if mode:
            return self._q("SELECT * FROM trades WHERE status='open' AND mode=? ORDER BY id", (mode,))
        return self._q("SELECT * FROM trades WHERE status='open' ORDER BY id")

    def closed_trades(self, limit: int = 500, mode: str | None = None) -> list[dict]:
        if mode:
            return self._q("SELECT * FROM trades WHERE status!='open' AND mode=? ORDER BY closed_at DESC LIMIT ?", (mode, limit))
        return self._q("SELECT * FROM trades WHERE status!='open' ORDER BY closed_at DESC LIMIT ?", (limit,))

    def last_exit_at(self, ticker: str, mode: str | None = None) -> str | None:
        """When this market was last closed out, or None if it never was.

        The entry rules measure a dip against a rolling high that does not decay, so a
        player in a sustained slide looks like a fresh dip at every new low. Without
        this, one decline is bought over and over.
        """
        args: tuple = (ticker,)
        sql = "SELECT closed_at FROM trades WHERE ticker=? AND status!='open' AND closed_at IS NOT NULL"
        if mode:
            sql += " AND mode=?"
            args += (mode,)
        rows = self._q(sql + " ORDER BY closed_at DESC LIMIT 1", args)
        return rows[0]["closed_at"] if rows else None

    def all_trades(self, limit: int = 1000) -> list[dict]:
        return self._q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    # --------------------------------------------------------------- equity
    def add_equity(self, *, mode: str, cash: float, positions_value: float, realized_pnl: float, unrealized_pnl: float, open_positions: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity(ts, mode, cash, positions_value, equity, realized_pnl, unrealized_pnl, open_positions) VALUES(?,?,?,?,?,?,?,?)",
                (utcnow(), mode, cash, positions_value, cash + positions_value, realized_pnl, unrealized_pnl, open_positions),
            )

    def equity_curve(self, limit: int = 2000, mode: str | None = None) -> list[dict]:
        if mode:
            rows = self._q("SELECT * FROM equity WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))
        else:
            rows = self._q("SELECT * FROM equity ORDER BY id DESC LIMIT ?", (limit,))
        rows.reverse()
        return rows

    def latest_equity(self, mode: str | None = None) -> dict | None:
        if mode:
            return self._one("SELECT * FROM equity WHERE mode=? ORDER BY id DESC LIMIT 1", (mode,))
        return self._one("SELECT * FROM equity ORDER BY id DESC LIMIT 1")

    # ------------------------------------------------------------ snapshots
    def add_snapshot(self, m: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO market_snapshots(ts, ticker, yes_bid, yes_ask, no_bid, no_ask, last_price, volume_24h, open_interest, volume)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), m["ticker"], m["yes_bid"], m["yes_ask"], m["no_bid"], m["no_ask"], m["last_price"],
                 m.get("volume_24h", 0), m.get("open_interest", 0), m.get("volume", 0)),
            )

    def snapshots(self, ticker: str, limit: int = 500) -> list[dict]:
        rows = self._q("SELECT * FROM market_snapshots WHERE ticker=? ORDER BY id DESC LIMIT ?", (ticker, limit))
        rows.reverse()
        return rows

    def latest_snapshots(self, limit: int = 100) -> list[dict]:
        return self._q(
            "SELECT s.* FROM market_snapshots s JOIN (SELECT ticker, MAX(id) AS mid FROM market_snapshots GROUP BY ticker) m"
            " ON s.id = m.mid ORDER BY s.volume_24h DESC LIMIT ?",
            (limit,),
        )

    # ----------------------------------------------------------------- runs
    def add_run(self, *, mode: str, markets_scanned: int, signals: int, trades_opened: int, trades_closed: int, duration_ms: int, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(ts, mode, markets_scanned, signals, trades_opened, trades_closed, duration_ms, error) VALUES(?,?,?,?,?,?,?,?)",
                (utcnow(), mode, markets_scanned, signals, trades_opened, trades_closed, duration_ms, error),
            )

    def recent_runs(self, limit: int = 20) -> list[dict]:
        return self._q("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
