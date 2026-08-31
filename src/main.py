"""FastAPI app: REST API + WebSocket activity stream + static dashboard.

Run with:  uvicorn src.main:app --reload
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .bot_engine import BotEngine
from .config import env_config
from .database import Database
from .kalshi_client import KalshiAPIError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

env_config.ensure_dirs()
db = Database(env_config.database_path)
engine = BotEngine(env_config, db)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await engine.startup()
    yield
    await engine.shutdown()


app = FastAPI(title="KalshiTrader", lifespan=lifespan)


# ── REST API ──────────────────────────────────────────────────────────


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    return engine.status()


@app.get("/api/overview")
async def get_overview() -> dict[str, Any]:
    if engine.last_overview:
        return engine.last_overview
    return {
        "env": engine.settings.env,
        "balance_cents": None,
        "exposure_cents": None,
        "equity_cents": None,
        "realized_pnl_cents": None,
        "unrealized_pnl_cents": None,
        "orders_placed": db.order_count(),
        "halted": engine.risk.halted,
        "halt_reason": engine.risk.halt_reason,
    }


@app.get("/api/equity_history")
async def get_equity_history(limit: int = 500) -> list[dict[str, Any]]:
    return db.equity_history(limit=min(limit, 2000))


@app.get("/api/orders")
async def get_orders(limit: int = 100) -> list[dict[str, Any]]:
    return db.recent_orders(limit=min(limit, 500))


@app.get("/api/fills")
async def get_fills(limit: int = 100) -> list[dict[str, Any]]:
    return db.recent_fills(limit=min(limit, 500))


@app.get("/api/activity")
async def get_activity(limit: int = 200) -> list[dict[str, Any]]:
    return db.recent_activity(limit=min(limit, 1000))


@app.get("/api/positions")
async def get_positions() -> dict[str, Any]:
    if engine.client is None:
        raise HTTPException(503, engine.client_error or "API client not configured")
    try:
        return await engine.client.get_positions()
    except KalshiAPIError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/markets")
async def get_markets(status: str = "open", series_ticker: str | None = None) -> dict[str, Any]:
    if engine.client is None:
        raise HTTPException(503, engine.client_error or "API client not configured")
    try:
        return await engine.client.get_markets(status=status, series_ticker=series_ticker)
    except KalshiAPIError as exc:
        raise HTTPException(502, str(exc)) from exc


# ── Settings ──────────────────────────────────────────────────────────


class SettingsPatch(BaseModel):
    env: str | None = None
    scan_interval_seconds: float | None = None
    contracts_per_side: int | None = None
    min_profit_cents: int | None = None
    edge_buffer_cents: int | None = None
    max_money_working_cents: int | None = None
    max_contracts_per_order: int | None = None
    daily_stop_loss_pct: float | None = None
    order_ttl_seconds: int | None = None
    arb_tickers: str | None = None
    arb_series: str | None = None
    fair_values: dict[str, int] | None = None
    confirm_live: bool = False


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return engine.settings.model_dump()


@app.put("/api/settings")
async def put_settings(patch: SettingsPatch) -> dict[str, Any]:
    data = patch.model_dump(exclude_none=True)
    confirm_live = data.pop("confirm_live", False)
    if data.get("env") == "live":
        if engine.settings.env != "live" and not confirm_live:
            raise HTTPException(
                400, "Switching to LIVE trading requires confirm_live=true"
            )
        key_id, _ = env_config.credentials_for("live")
        if not key_id:
            raise HTTPException(400, "No live credentials configured in .env")
    if data.get("env") not in (None, "demo", "live"):
        raise HTTPException(400, "env must be 'demo' or 'live'")
    settings = await engine.apply_settings(data)
    return settings.model_dump()


# ── Bot control ───────────────────────────────────────────────────────


@app.post("/api/bot/{strategy}/{command}")
async def bot_control(strategy: str, command: str) -> dict[str, Any]:
    if strategy not in engine.strategies:
        raise HTTPException(404, f"unknown strategy {strategy!r}")
    if command == "start":
        await engine.start_strategy(strategy)
    elif command == "pause":
        await engine.pause_strategy(strategy)
    elif command == "stop":
        await engine.stop_strategy(strategy)
    else:
        raise HTTPException(400, "command must be start | pause | stop")
    return engine.status()


@app.post("/api/risk/reset")
async def risk_reset() -> dict[str, Any]:
    engine.risk.reset()
    db.record_audit("risk_breaker_reset", {}, actor="dashboard")
    await engine.log("risk circuit breaker manually reset", "warn", "risk")
    return engine.status()


@app.post("/api/orders/cancel_all")
async def cancel_all() -> dict[str, int]:
    cancelled = await engine.cancel_all_orders()
    return {"cancelled": cancelled}


# ── WebSocket activity stream ─────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)

    def on_event(event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # slow client: drop rather than block the engine

    unsubscribe = engine.subscribe(on_event)
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()


# ── Static dashboard ──────────────────────────────────────────────────


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "index.html")


app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="dashboard")
