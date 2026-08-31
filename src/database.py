"""SQLite persistence via SQLAlchemy: orders, fills, equity curve, activity log,
audit trail, and the persisted bot settings blob."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .config import BotSettings


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    strategy: Mapped[str] = mapped_column(String(64))
    env: Mapped[str] = mapped_column(String(8))
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64))
    ticker: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))
    action: Mapped[str] = mapped_column(String(4))
    count: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="placed")
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FillRecord(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    env: Mapped[str] = mapped_column(String(8))
    fill_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(4))
    action: Mapped[str] = mapped_column(String(4))
    count: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    env: Mapped[str] = mapped_column(String(8))
    balance_cents: Mapped[int] = mapped_column(Integer)
    exposure_cents: Mapped[int] = mapped_column(Integer, default=0)
    equity_cents: Mapped[int] = mapped_column(Integer)
    realized_pnl_cents: Mapped[int] = mapped_column(Integer, default=0)
    unrealized_pnl_cents: Mapped[int] = mapped_column(Integer, default=0)


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    source: Mapped[str] = mapped_column(String(64), default="system")
    message: Mapped[str] = mapped_column(Text)


class AuditTrail(Base):
    __tablename__ = "audit_trail"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SettingsRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Database:
    def __init__(self, path: str):
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine)

    # ── Settings ──────────────────────────────────────────────────────

    def load_settings(self, default_env: str) -> BotSettings:
        with self.session() as s:
            row = s.get(SettingsRow, "bot_settings")
            if row is None:
                return BotSettings(env=default_env)
            return BotSettings(**json.loads(row.value))

    def save_settings(self, settings: BotSettings) -> None:
        with self.session() as s:
            row = s.get(SettingsRow, "bot_settings")
            payload = settings.model_dump_json()
            if row is None:
                s.add(SettingsRow(key="bot_settings", value=payload))
            else:
                row.value = payload
            s.commit()

    # ── Writes ────────────────────────────────────────────────────────

    def log_activity(self, message: str, level: str = "info", source: str = "system") -> ActivityLog:
        with self.session() as s:
            entry = ActivityLog(message=message, level=level, source=source)
            s.add(entry)
            s.commit()
            s.refresh(entry)
            return entry

    def record_audit(self, event: str, detail: dict[str, Any], actor: str = "system") -> None:
        with self.session() as s:
            s.add(AuditTrail(event=event, detail=detail, actor=actor))
            s.commit()

    def record_order(self, **kwargs: Any) -> None:
        with self.session() as s:
            s.add(OrderRecord(**kwargs))
            s.commit()

    def upsert_fill(self, **kwargs: Any) -> bool:
        """Insert a fill if unseen; returns True when newly recorded."""
        with self.session() as s:
            existing = s.execute(
                select(FillRecord).where(FillRecord.fill_id == kwargs["fill_id"])
            ).scalar_one_or_none()
            if existing is not None:
                return False
            s.add(FillRecord(**kwargs))
            s.commit()
            return True

    def record_equity(self, **kwargs: Any) -> None:
        with self.session() as s:
            s.add(EquitySnapshot(**kwargs))
            s.commit()

    # ── Reads for the dashboard ───────────────────────────────────────

    def recent_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(
                select(OrderRecord).order_by(OrderRecord.id.desc()).limit(limit)
            ).scalars()
            return [
                {
                    "time": r.created_at.isoformat(),
                    "strategy": r.strategy,
                    "env": r.env,
                    "order_id": r.order_id,
                    "ticker": r.ticker,
                    "side": r.side,
                    "action": r.action,
                    "count": r.count,
                    "price_cents": r.price_cents,
                    "status": r.status,
                }
                for r in rows
            ]

    def recent_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(
                select(FillRecord).order_by(FillRecord.id.desc()).limit(limit)
            ).scalars()
            return [
                {
                    "time": r.created_at.isoformat(),
                    "env": r.env,
                    "ticker": r.ticker,
                    "side": r.side,
                    "action": r.action,
                    "count": r.count,
                    "price_cents": r.price_cents,
                }
                for r in rows
            ]

    def order_count(self) -> int:
        with self.session() as s:
            return s.query(OrderRecord).count()

    def equity_history(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(
                select(EquitySnapshot).order_by(EquitySnapshot.id.desc()).limit(limit)
            ).scalars()
            out = [
                {
                    "time": r.created_at.isoformat(),
                    "equity_cents": r.equity_cents,
                    "balance_cents": r.balance_cents,
                    "exposure_cents": r.exposure_cents,
                }
                for r in rows
            ]
            return list(reversed(out))

    def recent_activity(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(
                select(ActivityLog).order_by(ActivityLog.id.desc()).limit(limit)
            ).scalars()
            out = [
                {
                    "time": r.created_at.isoformat(),
                    "level": r.level,
                    "source": r.source,
                    "message": r.message,
                }
                for r in rows
            ]
            return list(reversed(out))

    def first_equity_today(self, env: str) -> int | None:
        """Equity (cents) at the first snapshot taken today UTC, for the daily stop."""
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with self.session() as s:
            row = s.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.created_at >= start, EquitySnapshot.env == env)
                .order_by(EquitySnapshot.id.asc())
                .limit(1)
            ).scalar_one_or_none()
            return row.equity_cents if row else None
