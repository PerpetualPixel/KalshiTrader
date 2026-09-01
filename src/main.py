"""FastAPI app: REST API + WebSocket activity stream + static dashboard.

Run with:  uvicorn src.main:app --reload
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import COOKIE_NAME, AuthManager
from .bot_engine import BotEngine
from .config import env_config
from .database import Database
from .kalshi_client import KalshiAPIError
from .pnl import compute_pnl
from .strategies.base import OrderIntent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

env_config.ensure_dirs()
db = Database(env_config.database_path)
engine = BotEngine(env_config, db)
auth = AuthManager(db)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await engine.startup()
    yield
    await engine.shutdown()


app = FastAPI(title="KalshiTrader", lifespan=lifespan)

AUTH_EXEMPT_PATHS = {"/api/auth/status", "/api/auth/setup", "/api/auth/login"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every /api route except the auth handshake requires a valid session."""
    path = request.url.path
    if path.startswith("/api") and path not in AUTH_EXEMPT_PATHS:
        if not auth.verify_token(request.cookies.get(COOKIE_NAME)):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
    return await call_next(request)


# ── Auth ──────────────────────────────────────────────────────────────


class PasswordBody(BaseModel):
    password: str


def _set_session_cookie(response: Response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        auth.issue_token(),
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )


@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    return {
        "setup_required": not auth.is_configured(),
        "authenticated": auth.verify_token(request.cookies.get(COOKIE_NAME)),
    }


@app.post("/api/auth/setup")
async def auth_setup(body: PasswordBody, response: Response) -> dict[str, Any]:
    if auth.is_configured():
        raise HTTPException(409, "password already set — log in instead")
    try:
        auth.set_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.record_audit("auth_password_created", {}, actor="dashboard")
    _set_session_cookie(response)
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(body: PasswordBody, response: Response) -> dict[str, Any]:
    if not auth.is_configured():
        raise HTTPException(409, "no password set — run setup first")
    if not auth.verify_password(body.password):
        raise HTTPException(401, "incorrect password")
    _set_session_cookie(response)
    return {"ok": True}


@app.post("/api/auth/logout")
async def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


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


@app.get("/api/pnl")
async def get_pnl() -> dict[str, Any]:
    """Trade-level realized PnL: per-trade rows, cumulative curve, by-ticker."""
    return compute_pnl(db.fills_for_pnl(), db.settlements_for_pnl())


# ── API credentials (GUI-managed) ─────────────────────────────────────


class CredentialsBody(BaseModel):
    env: str
    key_id: str
    private_key_pem: str


@app.get("/api/credentials")
async def get_credentials() -> dict[str, Any]:
    return {"active_env": engine.settings.env, **engine.credentials.describe()}


@app.put("/api/credentials")
async def put_credentials(body: CredentialsBody) -> dict[str, Any]:
    try:
        engine.credentials.save(body.env, body.key_id, body.private_key_pem)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — bad PEM parse errors vary
        raise HTTPException(400, f"invalid private key: {exc}") from exc
    db.record_audit("credentials_updated", {"env": body.env}, actor="dashboard")
    result: dict[str, Any] = {"saved": True, "env": body.env}
    if body.env == engine.settings.env:
        ok, detail = await engine.rebuild_client()
        result["connection_ok"] = ok
        result["balance_cents"] = int(detail) if ok else None
        result["error"] = None if ok else detail
    return result


@app.delete("/api/credentials/{env}")
async def delete_credentials(env: str) -> dict[str, Any]:
    if env not in ("demo", "live"):
        raise HTTPException(400, "env must be 'demo' or 'live'")
    engine.credentials.clear(env)
    db.record_audit("credentials_cleared", {"env": env}, actor="dashboard")
    if env == engine.settings.env:
        await engine.rebuild_client()
    return {"cleared": True}


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
    swing_series: str | None = None
    swing_drop_cents: int | None = None
    swing_lookback_seconds: int | None = None
    swing_max_spread_cents: int | None = None
    swing_price_band_low: int | None = None
    swing_price_band_high: int | None = None
    swing_take_profit_cents: int | None = None
    swing_stop_loss_cents: int | None = None
    swing_max_hold_minutes: int | None = None
    swing_max_positions: int | None = None
    confirm_live: bool = False

    # A settings key the dashboard sends but this model forgets is dropped
    # silently by pydantic, so the setting simply never takes effect — that
    # is how the swing fields came to be missing here in the first place.
    # Reject unknown keys loudly instead, so the next field that goes missing
    # is a 422 rather than a mystery.
    model_config = {"extra": "forbid"}


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
        key_id, _ = engine.credentials.credentials_for("live")
        if not key_id:
            raise HTTPException(
                400,
                "No live credentials configured — add them in the API Credentials panel",
            )
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


@app.get("/api/debug/balance")
async def debug_balance() -> dict[str, Any]:
    """Raw balance response from Kalshi, including any per-shard breakdown."""
    if engine.client is None:
        raise HTTPException(503, engine.client_error or "API client not ready")
    return await engine.client.get_balance_raw()


@app.get("/api/debug/swing")
async def debug_swing() -> dict[str, Any]:
    """What the swing trader is tracking and why its last scan did or didn't
    fire — the gate settings, the per-gate skip tally, the closest candidate
    it saw, and the positions actually held on Kalshi."""
    strategy = engine.strategies.get("swing")
    if strategy is None or not hasattr(strategy, "debug_state"):
        raise HTTPException(404, "swing strategy not available")
    settings = engine.settings

    # Live positions are useful here but must not make the endpoint useless
    # when the client is down — that is exactly when it gets consulted.
    positions_held: dict[str, int] = {}
    positions_error: str | None = None
    if engine.client is None:
        positions_error = engine.client_error or "API client not ready"
    else:
        try:
            raw = await engine.client.get_positions()
            positions_held = {
                str(p.get("ticker", "")): int(p.get("position", 0) or 0)
                for p in raw.get("market_positions", [])
                if int(p.get("position", 0) or 0) != 0
            }
        except Exception as exc:  # noqa: BLE001 — diagnostics must still render
            positions_error = str(exc)

    return {
        "state": engine.strategy_state.get("swing"),
        "targets": settings.swing_series or f"(fallback) {settings.arb_series}",
        "gates": {
            "drop_cents": settings.swing_drop_cents,
            "lookback_seconds": settings.swing_lookback_seconds,
            "max_spread_cents": settings.swing_max_spread_cents,
            "price_band": [
                settings.swing_price_band_low,
                settings.swing_price_band_high,
            ],
            "max_positions": settings.swing_max_positions,
        },
        "positions_held": positions_held,
        "positions_error": positions_error,
        **strategy.debug_state(),
    }


class ManualOrderBody(BaseModel):
    ticker: str
    side: str  # yes | no
    action: str  # buy | sell
    count: int
    price_cents: int


@app.post("/api/manual_order")
async def manual_order(body: ManualOrderBody) -> dict[str, Any]:
    """Place a one-off order by hand. It flows through the same risk checks,
    order TTL, audit trail, and fill/PnL tracking as strategy orders."""
    if engine.client is None:
        raise HTTPException(503, engine.client_error or "API client not ready")
    if body.side not in ("yes", "no"):
        raise HTTPException(400, "side must be yes or no")
    if body.action not in ("buy", "sell"):
        raise HTTPException(400, "action must be buy or sell")
    if not 1 <= body.price_cents <= 99:
        raise HTTPException(400, "price must be 1-99 cents")
    if body.count < 1:
        raise HTTPException(400, "count must be at least 1")
    intent = OrderIntent(
        ticker=body.ticker.strip().upper(),
        side=body.side,
        action=body.action,
        count=body.count,
        price_cents=body.price_cents,
        reason="manual order from dashboard",
    )
    await engine._submit("manual", intent)
    return {"ok": True}


# ── WebSocket activity stream ─────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    if not auth.verify_token(ws.cookies.get(COOKIE_NAME)):
        await ws.close(code=4401, reason="not authenticated")
        return
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
