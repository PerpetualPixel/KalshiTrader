"""Application configuration.

Static credentials/server config come from the environment (.env).
Runtime-tunable bot settings (scan interval, sizing, thresholds) live in
`BotSettings` and are persisted to SQLite so the dashboard can edit them live.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

# Default API hosts per environment. Kalshi has moved hosts before
# (external-api.kalshi.com -> api.elections.kalshi.com), so both the current
# and legacy hosts are listed here and KALSHI_API_BASE overrides everything.
API_BASES = {
    "live": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
LEGACY_API_BASES = {
    "live": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}


class EnvConfig(BaseModel):
    """Process-level configuration loaded from environment variables."""

    env: str = "demo"
    key_id: str = ""
    private_key_path: str = "./keys/kalshi_private_key.pem"
    live_key_id: str | None = None
    live_private_key_path: str | None = None
    api_base_override: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "./data/kalshitrader.db"

    @classmethod
    def from_env(cls) -> "EnvConfig":
        return cls(
            env=os.getenv("KALSHI_ENV", "demo").strip().lower(),
            key_id=os.getenv("KALSHI_KEY_ID", "").strip(),
            private_key_path=os.getenv(
                "KALSHI_PRIVATE_KEY_PATH", "./keys/kalshi_private_key.pem"
            ).strip(),
            live_key_id=os.getenv("KALSHI_LIVE_KEY_ID") or None,
            live_private_key_path=os.getenv("KALSHI_LIVE_PRIVATE_KEY_PATH") or None,
            api_base_override=os.getenv("KALSHI_API_BASE") or None,
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")),
            database_path=os.getenv("DATABASE_PATH", "./data/kalshitrader.db"),
        )

    def credentials_for(self, env: str) -> tuple[str, str]:
        """Return (key_id, private_key_path) for the given environment."""
        if env == "live" and self.live_key_id and self.live_private_key_path:
            return self.live_key_id, self.live_private_key_path
        return self.key_id, self.private_key_path

    def api_base_for(self, env: str) -> str:
        if self.api_base_override:
            return self.api_base_override.rstrip("/")
        return API_BASES.get(env, API_BASES["demo"])

    def ensure_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)


class BotSettings(BaseModel):
    """Runtime-tunable settings, persisted in SQLite and editable in the UI.

    All money values are integer cents; all prices are Kalshi cents (1-99).
    """

    env: str = Field(default="demo", description="Active trading environment: demo | live")
    scan_interval_seconds: float = Field(default=10.0, ge=1.0, le=3600.0)
    contracts_per_side: int = Field(default=1, ge=1, le=1000)
    min_profit_cents: int = Field(
        default=3, ge=1, le=50,
        description="Arb edge threshold: act when yes_ask + no_ask <= 100 - this",
    )
    edge_buffer_cents: int = Field(
        default=3, ge=0, le=50,
        description="Fair-value strategy: required edge below fair value before bidding",
    )
    max_money_working_cents: int = Field(
        default=2500, ge=0,
        description="Hard ceiling on capital committed to open orders + positions",
    )
    max_contracts_per_order: int = Field(default=10, ge=1, le=10000)
    daily_stop_loss_pct: float = Field(
        default=5.0, ge=0.1, le=100.0,
        description="Halt trading and cancel open orders if daily loss exceeds this % of starting equity",
    )
    order_ttl_seconds: int = Field(
        default=60, ge=5, le=3600,
        description="expiration_ts applied to every limit order so nothing rests stale",
    )
    arb_tickers: str = Field(
        default="", description="Comma-separated market tickers for the arb scanner"
    )
    arb_series: str = Field(
        default="", description="Comma-separated series tickers to auto-discover markets from"
    )
    fair_values: dict[str, int] = Field(
        default_factory=dict,
        description="ticker -> fair YES probability in cents, for the fair-value strategy",
    )
    swing_series: str = Field(
        default="",
        description="Comma-separated series for the swing trader; falls back to the arb targets when empty",
    )
    swing_drop_cents: int = Field(
        default=10, ge=2, le=50,
        description="Ask must fall this much within the lookback window to trigger a dip buy",
    )
    swing_take_profit_cents: int = Field(
        default=5, ge=1, le=50,
        description="Sell a swing position once the bid recovers this far above entry",
    )
    swing_stop_loss_cents: int = Field(
        default=8, ge=1, le=50,
        description="Sell a swing position once the bid falls this far below entry",
    )
    swing_max_hold_minutes: int = Field(
        default=30, ge=1, le=1440,
        description="Time-exit: sell a swing position after holding this long",
    )
    swing_max_positions: int = Field(
        default=3, ge=1, le=50,
        description="Maximum concurrent swing positions",
    )

    def update(self, patch: dict) -> "BotSettings":
        data = self.model_dump()
        data.update({k: v for k, v in patch.items() if k in data})
        return BotSettings(**data)


env_config = EnvConfig.from_env()
