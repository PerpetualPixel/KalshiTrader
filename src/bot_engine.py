"""Bot engine: owns strategy loops, the Kalshi client, risk gating, equity
snapshots, fill syncing, and the live event stream consumed by the dashboard
WebSocket."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable

from .config import BotSettings, EnvConfig
from .credentials import CredentialStore
from .database import Database
from .kalshi_client import KalshiAPIError, KalshiClient
from .risk_manager import RiskManager
from .strategies import (
    ArbitrageStrategy,
    FairValueStrategy,
    SignalWatcherStrategy,
    Strategy,
    StrategyContext,
    SwingTraderStrategy,
)
from .strategies.base import OrderIntent

logger = logging.getLogger(__name__)

EQUITY_SNAPSHOT_INTERVAL = 30.0  # seconds
FILL_SYNC_INTERVAL = 15.0


class BotEngine:
    def __init__(self, env_config: EnvConfig, db: Database):
        self.env_config = env_config
        self.db = db
        self.settings: BotSettings = db.load_settings(env_config.env)
        self.credentials = CredentialStore(db, env_config)
        self.risk = RiskManager(self.settings)
        self.client: KalshiClient | None = None
        self.client_error: str | None = None

        self.strategies: dict[str, Strategy] = {
            s.name: s
            for s in (
                ArbitrageStrategy(),
                FairValueStrategy(),
                SwingTraderStrategy(),
                SignalWatcherStrategy(),
            )
        }
        # per-strategy state: stopped | running | paused
        self.strategy_state: dict[str, str] = {n: "stopped" for n in self.strategies}
        self._tasks: dict[str, asyncio.Task] = {}
        self._background: list[asyncio.Task] = []
        self._ttl_tasks: set[asyncio.Task] = set()
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self.last_overview: dict[str, Any] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def startup(self) -> None:
        self._build_client()
        self._background = [
            asyncio.create_task(self._equity_loop(), name="equity_loop"),
            asyncio.create_task(self._fill_sync_loop(), name="fill_sync_loop"),
        ]
        await self.log(
            f"engine started — env={self.settings.env}, "
            f"api={self._api_base()}", "info",
        )

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()) + self._background + list(self._ttl_tasks):
            task.cancel()
        if self.client:
            await self.client.close()

    def _api_base(self) -> str:
        return self.env_config.api_base_for(self.settings.env)

    def _build_client(self) -> None:
        if self.client is not None:
            asyncio.get_event_loop().create_task(self.client.close())
            self.client = None
        key_id, key_path = self.credentials.credentials_for(self.settings.env)
        try:
            if not key_id:
                raise ValueError(
                    "no API credentials — add them in the dashboard's API "
                    "Credentials panel or set KALSHI_KEY_ID in .env"
                )
            self.client = KalshiClient(self._api_base(), key_id, key_path)
            self.client_error = None
        except Exception as exc:  # noqa: BLE001 — surface config problems in the UI
            self.client_error = str(exc)
            logger.error("failed to build Kalshi client: %s", exc)

    # ── Event stream ──────────────────────────────────────────────────

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, event: dict[str, Any]) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                logger.exception("subscriber callback failed")

    async def log(self, message: str, level: str = "info", source: str = "engine") -> None:
        entry = self.db.log_activity(message, level=level, source=source)
        self._emit(
            {
                "type": "activity",
                "time": entry.created_at.isoformat(),
                "level": level,
                "source": source,
                "message": message,
            }
        )

    # ── Settings ──────────────────────────────────────────────────────

    async def apply_settings(self, patch: dict[str, Any], actor: str = "dashboard") -> BotSettings:
        old_env = self.settings.env
        self.settings = self.settings.update(patch)
        self.db.save_settings(self.settings)
        self.risk.update_settings(self.settings)
        self.db.record_audit("settings_updated", patch, actor=actor)
        if self.settings.env != old_env:
            await self.log(
                f"environment switched {old_env} -> {self.settings.env}; rebuilding client",
                "warn",
            )
            self._build_client()
        await self.log(f"settings updated: {', '.join(patch.keys())}")
        return self.settings

    # ── Strategy control ──────────────────────────────────────────────

    async def start_strategy(self, name: str) -> None:
        if name not in self.strategies:
            raise KeyError(name)
        state = self.strategy_state[name]
        if state == "running":
            return
        if state == "paused":
            self.strategy_state[name] = "running"
            await self.log(f"{name} resumed")
            return
        self.strategy_state[name] = "running"
        self._tasks[name] = asyncio.create_task(self._run_strategy(name), name=f"strategy_{name}")
        self.db.record_audit("strategy_started", {"strategy": name})
        await self.log(f"{name} started")

    async def pause_strategy(self, name: str) -> None:
        if self.strategy_state.get(name) == "running":
            self.strategy_state[name] = "paused"
            await self.log(f"{name} paused")

    async def stop_strategy(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()
        if self.strategy_state.get(name) != "stopped":
            self.strategy_state[name] = "stopped"
            self.db.record_audit("strategy_stopped", {"strategy": name})
            await self.log(f"{name} stopped")

    async def _run_strategy(self, name: str) -> None:
        strategy = self.strategies[name]
        while True:
            try:
                if self.strategy_state.get(name) == "running":
                    if self.client is None:
                        await self.log(
                            f"{name}: no API client ({self.client_error}); idle", "warn"
                        )
                    else:
                        ctx = StrategyContext(
                            client=self.client,
                            settings=self.settings,
                            log=lambda m, lvl="info": self.log(m, lvl, source=name),
                        )
                        await self.log(f"{name}: scanning…", "scan", source=name)
                        intents = await strategy.scan_once(ctx)
                        for intent in intents:
                            await self._submit(name, intent)
            except asyncio.CancelledError:
                raise
            except KalshiAPIError as exc:
                await self.log(f"{name}: API error: {exc}", "error", source=name)
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                logger.exception("strategy %s crashed a scan", name)
                await self.log(f"{name}: scan error: {exc}", "error", source=name)
            await asyncio.sleep(max(1.0, self.settings.scan_interval_seconds))

    # ── Order submission ──────────────────────────────────────────────

    async def _money_working_cents(self) -> int:
        """Capital committed to resting orders + open positions, in cents."""
        assert self.client is not None
        total = 0
        orders = await self.client.get_orders(status="resting")
        for o in orders:
            price = o.get("yes_price") if o.get("side") == "yes" else o.get("no_price")
            remaining = o.get("remaining_count", o.get("count", 0)) or 0
            total += int(price or 0) * int(remaining)
        positions = await self.client.get_positions()
        for p in positions.get("market_positions", []):
            total += abs(int(p.get("market_exposure", 0)))
        return total

    async def _submit(self, strategy_name: str, intent: OrderIntent) -> None:
        assert self.client is not None
        try:
            working = await self._money_working_cents()
        except KalshiAPIError as exc:
            await self.log(f"could not compute working capital, skipping order: {exc}", "error")
            return

        decision = self.risk.check_order(
            intent.count, intent.price_cents, working, action=intent.action
        )
        if not decision.allowed:
            await self.log(f"RISK BLOCK [{intent.ticker}]: {decision.reason}", "warn", "risk")
            return

        client_order_id = str(uuid.uuid4())
        expiration_ts = int(time.time()) + self.settings.order_ttl_seconds
        try:
            order = await self.client.place_order(
                ticker=intent.ticker,
                side=intent.side,
                action=intent.action,
                count=intent.count,
                price_cents=intent.price_cents,
                expiration_ts=expiration_ts,
                client_order_id=client_order_id,
            )
        except KalshiAPIError as exc:
            await self.log(
                f"ORDER REJECTED [{intent.ticker} {intent.action} {intent.count} "
                f"{intent.side}@{intent.price_cents}c]: {exc}",
                "error", strategy_name,
            )
            return

        self.db.record_order(
            strategy=strategy_name,
            env=self.settings.env,
            order_id=str(order.get("order_id", "")),
            client_order_id=client_order_id,
            ticker=intent.ticker,
            side=intent.side,
            action=intent.action,
            count=intent.count,
            price_cents=intent.price_cents,
            status=str(order.get("status", "placed")),
            raw=order,
        )
        self.db.record_audit(
            "order_placed",
            {"strategy": strategy_name, "ticker": intent.ticker, "side": intent.side,
             "count": intent.count, "price_cents": intent.price_cents,
             "reason": intent.reason, "order_id": order.get("order_id")},
        )
        await self.log(
            f"ORDER PLACED [{intent.ticker}] {intent.action} {intent.count} "
            f"{intent.side} @ {intent.price_cents}c (ttl {self.settings.order_ttl_seconds}s) "
            f"— {intent.reason}",
            "order", strategy_name,
        )
        self._emit({"type": "orders_changed"})

        # Kalshi's V2 order API has no timed expiration, so enforce the TTL
        # ourselves: cancel whatever is still resting once it elapses.
        order_id = str(order.get("order_id", ""))
        if order_id:
            task = asyncio.create_task(
                self._expire_order(order_id, self.settings.order_ttl_seconds)
            )
            self._ttl_tasks.add(task)
            task.add_done_callback(self._ttl_tasks.discard)

    async def _expire_order(self, order_id: str, ttl_seconds: int) -> None:
        await asyncio.sleep(max(1, ttl_seconds))
        if self.client is None:
            return
        try:
            await self.client.cancel_order(order_id)
            await self.log(f"order {order_id} expired (TTL) and was cancelled", "info")
            self._emit({"type": "orders_changed"})
        except KalshiAPIError:
            pass  # already filled or cancelled — nothing to do

    async def cancel_all_orders(self) -> int:
        """Cancel every resting order. Returns how many were cancelled."""
        if self.client is None:
            return 0
        cancelled = 0
        try:
            for order in await self.client.get_orders(status="resting"):
                oid = order.get("order_id")
                if not oid:
                    continue
                try:
                    await self.client.cancel_order(oid)
                    cancelled += 1
                except KalshiAPIError as exc:
                    await self.log(f"cancel failed for {oid}: {exc}", "error")
        except KalshiAPIError as exc:
            await self.log(f"could not list resting orders: {exc}", "error")
        if cancelled:
            await self.log(f"cancelled {cancelled} resting order(s)", "warn")
        return cancelled

    # ── Background loops ──────────────────────────────────────────────

    async def _equity_loop(self) -> None:
        while True:
            try:
                await self._snapshot_equity()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("equity snapshot failed: %s", exc)
            await asyncio.sleep(EQUITY_SNAPSHOT_INTERVAL)

    async def _snapshot_equity(self) -> None:
        if self.client is None:
            return
        balance = await self.client.get_balance()
        positions = await self.client.get_positions()
        exposure = 0
        realized = 0
        unrealized = 0
        for p in positions.get("market_positions", []):
            exposure += abs(int(p.get("market_exposure", 0)))
            realized += int(p.get("realized_pnl", 0))
            # Kalshi does not return mark-to-market directly; exposure is cost
            # basis of open contracts. Unrealized PnL is approximated as 0 at
            # cost until settlement moves it into realized.
        equity = balance + exposure
        self.db.record_equity(
            env=self.settings.env,
            balance_cents=balance,
            exposure_cents=exposure,
            equity_cents=equity,
            realized_pnl_cents=realized,
            unrealized_pnl_cents=unrealized,
        )
        self.last_overview = {
            "env": self.settings.env,
            "balance_cents": balance,
            "exposure_cents": exposure,
            "equity_cents": equity,
            "realized_pnl_cents": realized,
            "unrealized_pnl_cents": unrealized,
            "orders_placed": self.db.order_count(),
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
        }
        self._emit({"type": "overview", **self.last_overview})

        day_start = self.db.first_equity_today(self.settings.env)
        if self.risk.check_daily_stop(day_start, equity):
            await self.log(
                f"CIRCUIT BREAKER TRIPPED — {self.risk.halt_reason}. "
                "Cancelling open orders and pausing all strategies.",
                "error", "risk",
            )
            await self.cancel_all_orders()
            for name in self.strategies:
                await self.pause_strategy(name)

    async def rebuild_client(self) -> tuple[bool, str]:
        """Rebuild the API client (after credential changes) and test it.

        Returns (ok, detail) where detail is a balance string or an error.
        """
        self._build_client()
        if self.client is None:
            return False, self.client_error or "client could not be built"
        try:
            balance = await self.client.get_balance()
        except KalshiAPIError as exc:
            return False, str(exc)
        await self.log(
            f"API connection verified — balance ${balance / 100:.2f} "
            f"({self.settings.env})",
            "info",
        )
        return True, f"{balance}"

    async def _fill_sync_loop(self) -> None:
        while True:
            try:
                if self.client is not None:
                    changed = await self._sync_fills()
                    changed |= await self._sync_settlements()
                    if changed:
                        self._emit({"type": "trades_changed"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("fill sync failed: %s", exc)
            await asyncio.sleep(FILL_SYNC_INTERVAL)

    async def _sync_fills(self) -> bool:
        assert self.client is not None
        changed = False
        for fill in await self.client.get_fills(limit=100):
            side = str(fill.get("side", ""))
            is_new = self.db.upsert_fill(
                env=self.settings.env,
                fill_id=str(fill.get("trade_id") or fill.get("fill_id") or ""),
                order_id=str(fill.get("order_id", "")),
                ticker=str(fill.get("ticker", "")),
                side=side,
                action=str(fill.get("action", "")),
                count=int(fill.get("count", 0)),
                price_cents=int(
                    (fill.get("yes_price") if side == "yes" else fill.get("no_price"))
                    or 0
                ),
                raw=fill,
            )
            if is_new:
                changed = True
                await self.log(
                    f"FILL [{fill.get('ticker')}] {fill.get('action')} "
                    f"{fill.get('count')} {fill.get('side')} @ "
                    f"{(fill.get('yes_price') if side == 'yes' else fill.get('no_price')) or '?'}c",
                    "fill", "fills",
                )
        return changed

    async def _sync_settlements(self) -> bool:
        assert self.client is not None
        changed = False
        try:
            settlements = await self.client.get_settlements(limit=100)
        except KalshiAPIError:
            return False  # endpoint not critical; retry next cycle
        for st in settlements:
            ticker = str(st.get("ticker", ""))
            key = f"{ticker}:{st.get('settled_time', st.get('settled_ts', ''))}"
            is_new = self.db.upsert_settlement(
                env=self.settings.env,
                settlement_key=key,
                ticker=ticker,
                market_result=str(st.get("market_result", "")),
                revenue_cents=int(st.get("revenue", 0) or 0),
                raw=st,
            )
            if is_new:
                changed = True
                await self.log(
                    f"SETTLED [{ticker}] result={st.get('market_result')} "
                    f"revenue {int(st.get('revenue', 0) or 0)}c",
                    "fill", "settlements",
                )
        return changed

    # ── Status for the API ────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "env": self.settings.env,
            "api_base": self._api_base(),
            "client_ready": self.client is not None,
            "client_error": self.client_error,
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "strategies": {
                name: {
                    "label": self.strategies[name].label,
                    "state": self.strategy_state[name],
                    "places_orders": self.strategies[name].places_orders,
                }
                for name in self.strategies
            },
        }
