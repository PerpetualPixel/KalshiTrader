"""FastAPI dashboard: JSON API over the tracking store plus two single-page UIs.

  /       simple tennis view: positions, watched matches, settings, guide, credentials
  /full   the detailed tracking dashboard (equity curve, every signal, every trade)
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from kalshitrader import __version__
from kalshitrader.analysis.estimators import ManualEstimator
from kalshitrader.analysis.ev import kalshi_fee_cents
from kalshitrader.config import (
    PROFILES,
    TUNABLE_KEYS,
    TUNABLES,
    Settings,
    apply_overrides,
    load_settings,
    profile_overrides,
    update_env_file,
)
from kalshitrader.kalshi.client import KalshiClient, KalshiError
from kalshitrader.markets.discovery import categorise, sweep
from kalshitrader.paths import asset
from kalshitrader.tracking.db import Store
from kalshitrader.tracking.metrics import compute_metrics

# Resolved through `paths` so the dashboard still finds its files when the app is
# shipped as a single executable, where the package lives in a temporary unpack dir.
STATIC = asset("dashboard", "static")


class EstimateIn(BaseModel):
    ticker: str = Field(..., min_length=1)
    p_yes: float = Field(..., ge=0.0, le=1.0)
    confidence: float = Field(0.8, ge=0.0, le=1.0)
    note: str = ""


class CredentialsIn(BaseModel):
    kalshi_api_key_id: str | None = None
    kalshi_private_key_pem: str | None = None
    anthropic_api_key: str | None = None
    kalshi_env: str | None = Field(None, pattern="^(demo|prod)$")
    trading_mode: str | None = Field(None, pattern="^(paper|live)$")


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    return ("…" + value[-4:]) if len(value) > 4 else "set"


def _load_or_degrade(env_file: str | Path) -> tuple[Settings, str | None]:
    """Load .env, falling back to a safe paper-mode config if it is invalid.

    Setting Execution=live without a usable private key would otherwise stop the
    dashboard from starting - exactly when the user needs it to fix the keys.
    """
    try:
        return load_settings(env_file), None
    except ValueError as exc:
        import os

        os.environ["TRADING_MODE"] = "paper"
        try:
            safe = load_settings(env_file)
        except ValueError:
            safe = Settings()
        finally:
            os.environ.pop("TRADING_MODE", None)
        safe.trading_mode = "paper"
        return safe, str(exc)


def create_app(settings: Settings | None = None, store: Store | None = None, env_file: str | Path = ".env") -> FastAPI:
    if settings is None:
        settings, _ = _load_or_degrade(env_file)
    store = store or Store(settings.db_path)
    manual = ManualEstimator(settings.manual_estimates_path)
    app = FastAPI(title="KalshiTrader", version=__version__)
    app.state.store = store
    app.state.settings = settings

    def effective_settings() -> tuple[Settings, str | None]:
        s, err = _load_or_degrade(env_file)
        return apply_overrides(s, store.get_state("settings_overrides", {})), err

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/full", response_class=HTMLResponse)
    def full() -> str:
        return (STATIC / "full.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    # ---------------------------------------------------------------- tennis
    @app.get("/api/tennis/overview")
    def tennis_overview() -> dict:
        s, config_error = effective_settings()
        trades = store.all_trades(limit=100000)
        equity = store.equity_curve(limit=100000)
        starting = store.get_state("paper_starting_cash") if not s.is_live else None
        metrics = compute_metrics(trades, equity, starting)
        positions = []
        for t in store.open_trades():
            snap = store.snapshots(t["ticker"], limit=1)
            now_bid = None
            if snap:
                now_bid = snap[-1]["yes_bid"] if t["side"] == "yes" else snap[-1]["no_bid"]
            cost = t["entry_price"] * t["count"] / 100
            value = (now_bid or t["entry_price"]) * t["count"] / 100
            paid = float(t.get("fees") or 0)  # entry fee, already charged
            # What this position pays and costs in dollars, so the size of a bet is
            # legible without doing cents-times-contracts in your head. Both are net
            # of the exit fee Kalshi will charge on the way out.
            tp, sl = t.get("take_profit"), t.get("stop_loss")
            target_payout = (tp * t["count"] / 100) if tp else None
            target_gain = (target_payout - cost - paid - kalshi_fee_cents(tp, s.fee_rate, t["count"]) / 100) if tp else None
            stop_payout = (sl * t["count"] / 100) if sl else None
            # No stop means the position rides to settlement, where a loser pays zero:
            # the stake is the loss, and no exit fee is charged on a worthless contract.
            at_risk = (cost + paid + kalshi_fee_cents(sl, s.fee_rate, t["count"]) / 100 - stop_payout) if sl else cost + paid
            positions.append({**t, "now_bid": now_bid, "cost": cost, "unrealized": value - cost - paid,
                              "target_payout": target_payout, "target_gain": target_gain,
                              "stop_payout": stop_payout, "at_risk": at_risk})
        return {
            "mode": s.trading_mode, "env": s.kalshi_env, "bot_enabled": s.bot_enabled,
            "halted": bool(store.get_state("halted", False)), "last_cycle": store.get_state("last_cycle"),
            "research_status": store.get_state("research_status", "unknown"),
            "research_pending": store.get_state("research_pending", 0),
            "kalshi_key_set": bool(s.kalshi_api_key_id and (s.kalshi_private_key_pem or s.kalshi_private_key_path)),
            "discovery": store.get_state("tennis_discovery", {}), "series": s.enabled_series,
            "counts": store.get_state("tennis_counts", {}), "scan_interval": s.scan_interval_seconds,
            "form_status": store.get_state("form_status", ""), "active_profile": store.get_state("active_profile"),
            "blockers": store.get_state("tennis_blockers", []),
            "config_error": config_error,
            "equity": metrics["equity"], "cash": metrics["cash"], "realized_pnl": metrics["realized_pnl"],
            "daily_pnl": metrics["daily_pnl"], "win_rate": metrics["win_rate"], "trades_closed": metrics["trades_closed"],
            "positions": positions, "watch": store.get_state("tennis_watch", []),
            "open_cost": sum(p["cost"] for p in positions),
            "open_at_risk": sum(p["at_risk"] for p in positions if p["at_risk"] is not None),
            "open_target_gain": sum(p["target_gain"] for p in positions if p["target_gain"] is not None),
            "recent_closed": store.closed_trades(limit=8), "version": __version__,
        }

    @app.post("/api/positions/{trade_id}/close")
    async def close_position(trade_id: int) -> dict:
        """Ask the trading loop to sell this position on its next scan.

        The dashboard holds no broker: placing the order here would need a second
        set of credentials and could race the loop into selling the same position
        twice. So this records the request and the loop, which owns execution,
        fills it - normally within one scan interval.
        """
        s2, _ = _load_or_degrade(env_file)
        if not store.request_close(trade_id):
            raise HTTPException(status_code=404, detail="that position is not open (it may have just closed)")
        return {"ok": True, "trade_id": trade_id,
                "detail": f"Selling on the bot's next scan (up to {s2.scan_interval_seconds}s). The bot must be running."}

    if _multipart_available():

        @app.post("/api/tennis/screenshot")
        async def tennis_screenshot(file: Annotated[UploadFile, File()]) -> dict:
            from kalshitrader.tennis.screenshot import analyze_screenshot_bytes

            s, _ = effective_settings()
            data = await file.read()
            suffix = Path(file.filename or "shot.png").suffix or ".png"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                path = tmp.name
            try:
                return analyze_screenshot_bytes(path, s, store)
            except ImportError as exc:
                raise HTTPException(400, str(exc)) from exc
            finally:
                Path(path).unlink(missing_ok=True)

    else:

        @app.post("/api/tennis/screenshot")
        async def tennis_screenshot_unavailable() -> dict:
            raise HTTPException(503, "screenshot upload needs the python-multipart package: run  pip install -e .  and restart the dashboard")

    # --------------------------------------------------------------- settings
    @app.get("/api/settings/schema")
    def settings_schema() -> list[dict]:
        s, _ = effective_settings()
        return [{**t, "value": getattr(s, t["key"])} for t in TUNABLES]

    @app.put("/api/settings")
    def put_settings(body: dict) -> dict:
        unknown = [k for k in body if k not in TUNABLE_KEYS]
        if unknown:
            raise HTTPException(400, f"unknown settings: {', '.join(unknown)}")
        overrides = store.get_state("settings_overrides", {})
        overrides.update(body)
        store.set_state("active_profile", None)  # hand-edited: no longer a clean profile
        # validate by applying to a scratch copy
        apply_overrides(load_settings(env_file) if Path(env_file).exists() else Settings(), overrides)
        store.set_state("settings_overrides", overrides)
        return {"ok": True, "overrides": overrides}

    @app.get("/api/profiles")
    def list_profiles() -> list[dict]:
        active = store.get_state("active_profile")
        return [{"name": name, "label": p["label"], "blurb": p["blurb"], "active": name == active,
                 "settings": p["settings"]} for name, p in PROFILES.items()]

    @app.get("/api/series")
    def markets_available() -> dict:
        """Every series the last sweep found, and which of them are switched on."""
        s2, _ = _load_or_degrade(env_file)
        apply_overrides(s2, store.get_state("settings_overrides", {}))
        found = store.get_state("series_discovered", [])
        enabled = {t.upper() for t in s2.enabled_series}
        rows = [{**row, "enabled": row["ticker"].upper() in enabled} for row in found]
        # A series someone switched on before the sweep ran, or that has since gone
        # quiet, still has to appear - otherwise the toggle for it silently vanishes.
        for ticker in sorted(enabled - {r["ticker"].upper() for r in rows}):
            rows.append({"ticker": ticker, "category": categorise(ticker), "markets": 0,
                         "open_markets": 0, "volume_24h": 0, "open_interest": 0,
                         "liquid_markets": 0, "examples": [], "enabled": True})
        return {"series": rows, "enabled": sorted(enabled), "prefixes": s2.series_prefixes,
                "scanned_at": store.get_state("series_discovered_at"),
                "categories": sorted({r["category"] for r in rows})}

    @app.post("/api/series/discover")
    def markets_discover() -> dict:
        """Sweep the exchange for what is actually trading right now."""
        s2, err = _load_or_degrade(env_file)
        client = KalshiClient(s2.base_url, api_key_id=s2.kalshi_api_key_id,
                              private_key_path=s2.kalshi_private_key_path,
                              private_key_pem=s2.kalshi_private_key_pem)
        try:
            series = sweep(client, pages=s2.discovery_pages, limit=1000)
        except KalshiError as exc:
            raise HTTPException(502, f"Kalshi refused the scan: {exc}") from exc
        rows = [s.as_dict() for s in series]
        store.set_state("series_discovered", rows)
        store.set_state("series_discovered_at", datetime.now(timezone.utc).isoformat())
        return {"ok": True, "found": len(rows),
                "detail": f"Found {len(rows)} series. Switch on the ones you want to trade."}

    @app.put("/api/series")
    async def markets_set(request: Request) -> dict:
        """Replace the list of series the bot may trade."""
        body = await request.json()
        tickers = body.get("enabled_series")
        if not isinstance(tickers, list) or not all(isinstance(x, str) for x in tickers):
            raise HTTPException(400, "enabled_series must be a list of series tickers")
        cleaned = sorted({x.strip().upper() for x in tickers if x.strip()})
        current = store.get_state("settings_overrides", {})
        current["enabled_series"] = cleaned
        store.set_state("settings_overrides", current)
        return {"ok": True, "enabled_series": cleaned,
                "detail": f"{len(cleaned)} market{'' if len(cleaned) == 1 else 's'} switched on. Applies on the next scan."}

    @app.post("/api/profiles/{name}")
    def apply_profile(name: str) -> dict:
        try:
            overrides = profile_overrides(name)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        current = store.get_state("settings_overrides", {})
        # A profile sets the entry and exit knobs only; it never touches sizing, the
        # pause switch or your keys, so switching profile cannot change what is at risk.
        current.update(overrides)
        store.set_state("settings_overrides", current)
        store.set_state("active_profile", name.lower())
        return {"ok": True, "profile": name.lower(), "applied": overrides}

    @app.delete("/api/settings")
    def reset_settings() -> dict:
        store.set_state("settings_overrides", {})
        store.set_state("active_profile", None)
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings() -> dict:
        return effective_settings()[0].as_public_dict()

    # ------------------------------------------------------------ credentials
    @app.get("/api/credentials")
    def get_credentials() -> dict:
        s, _ = effective_settings()
        import os

        return {
            "kalshi_api_key_id": _mask(s.kalshi_api_key_id),
            "kalshi_private_key": "set" if (s.kalshi_private_key_pem or (s.kalshi_private_key_path and Path(s.kalshi_private_key_path).exists())) else None,
            "anthropic_api_key": _mask(os.environ.get("ANTHROPIC_API_KEY") or _env_value(env_file, "ANTHROPIC_API_KEY")),
            "kalshi_env": s.kalshi_env, "trading_mode": s.trading_mode, "env_file": str(env_file),
        }

    @app.post("/api/credentials/test")
    def test_credentials() -> dict:
        """Call Kalshi with the saved key and report the balance, or the exact error."""
        import importlib.util
        import os

        from kalshitrader.kalshi.client import KalshiClient, KalshiError

        s, _ = effective_settings()
        out: dict = {"env": s.kalshi_env, "base_url": s.base_url}
        try:
            client = KalshiClient(s.base_url, api_key_id=s.kalshi_api_key_id, private_key_path=s.kalshi_private_key_path,
                                  private_key_pem=s.kalshi_private_key_pem, timeout=10.0, max_retries=0)
        except Exception as exc:
            out["kalshi"] = {"ok": False, "error": f"private key could not be loaded: {exc}"}
            client = None
        if client is not None:
            try:
                markets = client.get_markets(limit=1)
                out["market_data"] = {"ok": True, "sample": markets[0].ticker if markets else None}
            except Exception as exc:
                out["market_data"] = {"ok": False, "error": str(exc)[:200]}
            if not client.authenticated:
                if s.kalshi_api_key_id and not (s.kalshi_private_key_pem or s.kalshi_private_key_path):
                    msg = "key ID saved, but no private key: paste the PEM file contents and save"
                elif (s.kalshi_private_key_pem or s.kalshi_private_key_path) and not s.kalshi_api_key_id:
                    msg = "private key saved, but no key ID"
                else:
                    msg = "no API key saved yet"
                out["kalshi"] = {"ok": False, "error": msg}
            else:
                try:
                    bal = client.get_balance()
                    out["kalshi"] = {"ok": True, "balance": bal.get("balance", 0) / 100, "portfolio_value": bal.get("portfolio_value", 0) / 100}
                except KalshiError as exc:
                    hint = " (key rejected: check the key ID matches this exchange, demo keys do not work on prod and vice versa)" if exc.status in (401, 403) else ""
                    out["kalshi"] = {"ok": False, "error": f"{exc}{hint}"}
                except Exception as exc:
                    out["kalshi"] = {"ok": False, "error": str(exc)[:200]}
            client.close()
        has_pkg = importlib.util.find_spec("anthropic") is not None
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or _env_value(env_file, "ANTHROPIC_API_KEY"))
        out["anthropic"] = {"ok": has_pkg and has_key, "package_installed": has_pkg, "key_saved": has_key,
                            "error": None if has_pkg and has_key else ("anthropic package not installed: run  pip install -e .[ai]" if not has_pkg else "no Anthropic key saved")}
        return out

    @app.post("/api/credentials")
    def set_credentials(body: CredentialsIn) -> dict:
        updates: dict[str, str | None] = {}
        if body.kalshi_api_key_id is not None:
            updates["KALSHI_API_KEY_ID"] = body.kalshi_api_key_id.strip() or None
        if body.kalshi_private_key_pem is not None:
            pem = body.kalshi_private_key_pem.strip()
            if pem and "PRIVATE KEY" not in pem:
                raise HTTPException(400, "that does not look like a PEM private key")
            updates["KALSHI_PRIVATE_KEY_PEM"] = pem or None
        if body.anthropic_api_key is not None:
            updates["ANTHROPIC_API_KEY"] = body.anthropic_api_key.strip() or None
        if body.kalshi_env:
            updates["KALSHI_ENV"] = body.kalshi_env
        if body.trading_mode:
            updates["TRADING_MODE"] = body.trading_mode
        if not updates:
            raise HTTPException(400, "nothing to update")
        update_env_file(env_file, updates)
        if "KALSHI_PRIVATE_KEY_PEM" in updates and updates["KALSHI_PRIVATE_KEY_PEM"]:
            try:
                from kalshitrader.kalshi.auth import load_private_key

                load_private_key(pem=updates["KALSHI_PRIVATE_KEY_PEM"])
            except Exception as exc:
                update_env_file(env_file, {"KALSHI_PRIVATE_KEY_PEM": None})
                raise HTTPException(400, f"private key could not be parsed: {exc}") from exc
        return {"ok": True, "updated": sorted(updates)}

    # ---------------------------------------------------------- full dashboard
    @app.get("/api/summary")
    def summary() -> dict:
        s, _ = effective_settings()
        trades = store.all_trades(limit=100000)
        equity = store.equity_curve(limit=100000)
        starting = store.get_state("paper_starting_cash") if not s.is_live else None
        metrics = compute_metrics(trades, equity, starting)
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        return {
            "mode": s.trading_mode, "env": s.kalshi_env, "halted": bool(store.get_state("halted", False)),
            "last_cycle": store.get_state("last_cycle"), "metrics": metrics, "signals_24h": store.signal_counts(since),
            "starting_cash": starting, "version": __version__,
        }

    @app.get("/api/equity")
    def equity(limit: int = 2000) -> list[dict]:
        return store.equity_curve(limit=limit)

    @app.get("/api/trades")
    def trades(status: str = "all", limit: int = 200) -> list[dict]:
        if status == "open":
            return store.open_trades()
        if status == "closed":
            return store.closed_trades(limit=limit)
        return store.all_trades(limit=limit)

    @app.get("/api/signals")
    def signals(limit: int = 100, trades_only: bool = False) -> list[dict]:
        return store.recent_signals(limit=limit, trades_only=trades_only)

    @app.get("/api/orders")
    def orders(limit: int = 100) -> list[dict]:
        return store.recent_orders(limit=limit)

    @app.get("/api/runs")
    def runs(limit: int = 30) -> list[dict]:
        return store.recent_runs(limit=limit)

    @app.get("/api/markets")
    def markets(limit: int = 100) -> list[dict]:
        return store.latest_snapshots(limit=limit)

    @app.get("/api/markets/{ticker}/history")
    def market_history(ticker: str, limit: int = 500) -> list[dict]:
        return store.snapshots(ticker.upper(), limit=limit)

    @app.get("/api/estimates")
    def estimates() -> dict:
        return manual.all()

    @app.post("/api/estimates")
    def set_estimate(body: EstimateIn) -> dict:
        manual.set(body.ticker, body.p_yes, body.confidence, body.note)
        return {"ok": True, "ticker": body.ticker.upper()}

    @app.delete("/api/estimates/{ticker}")
    def delete_estimate(ticker: str) -> dict:
        if not manual.remove(ticker):
            raise HTTPException(404, "no estimate for that ticker")
        return {"ok": True}

    @app.post("/api/control/halt")
    def halt() -> dict:
        store.set_state("halted", True)
        return {"halted": True}

    @app.post("/api/control/resume")
    def resume() -> dict:
        store.set_state("halted", False)
        return {"halted": False}

    return app


def _multipart_available() -> bool:
    import importlib.util

    return any(importlib.util.find_spec(name) is not None for name in ("python_multipart", "multipart"))


def _env_value(env_file: str | Path, key: str) -> str | None:
    from dotenv import dotenv_values

    return dotenv_values(env_file).get(key) if Path(env_file).exists() else None


def serve(settings: Settings | None = None, env_file: str = ".env") -> None:
    import uvicorn

    if settings is None:
        settings, _ = _load_or_degrade(env_file)
    uvicorn.run(create_app(settings, env_file=env_file), host=settings.dashboard_host, port=settings.dashboard_port, log_level="info")
