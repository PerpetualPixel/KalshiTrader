"""Runtime configuration, loaded from environment variables (and an optional .env file).

Every number that governs risk lives here so a reviewer can audit the whole
risk posture from one file. Prices are in cents, money is in dollars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import dotenv_values

from kalshitrader.paths import resolve_state_path

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


_VALUES: dict[str, str] = {}


def _env(name: str, default: str | None = None) -> str | None:
    val = _VALUES.get(name)
    return val if val not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    val = _env(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = _env(name)
    return int(val) if val is not None else default


def _env_float(name: str, default: float) -> float:
    val = _env(name)
    return float(val) if val is not None else default


def _env_list(name: str, default: list[str]) -> list[str]:
    val = _env(name)
    if val is None:
        return list(default)
    return [x.strip() for x in val.split(",") if x.strip()]


@dataclass
class Settings:
    # --- Kalshi API -------------------------------------------------------
    kalshi_env: str = "prod"  # "prod" (real market data; orders only in live mode) or "demo" (practice exchange, few markets)
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    kalshi_private_key_pem: str | None = None  # alternative to the path (e.g. in CI secrets)
    kalshi_base_url: str | None = None  # override; derived from kalshi_env when unset

    # --- Trading mode ----------------------------------------------------
    trading_mode: str = "paper"  # "paper" (simulated fills) or "live" (real orders)
    paper_starting_cash: float = 1000.0  # dollars
    paper_slippage_cents: int = 1

    # --- Market scan -----------------------------------------------------
    scan_interval_seconds: int = 60
    scan_limit: int = 200  # max markets pulled per scan
    series_tickers: list[str] = field(default_factory=list)  # empty = all open markets
    event_tickers: list[str] = field(default_factory=list)
    min_hours_to_close: float = 0.5
    max_hours_to_close: float = 24 * 14
    min_volume_24h: int = 50

    # --- Edge / EV -------------------------------------------------------
    min_edge_cents: float = 3.5  # EV per contract after BOTH fees, required to trade
    # Floor on reward:risk measured AFTER both fees. 0 disables it, which is the
    # default because no dip these profiles accept can reach 1:1 net: at a 14c dip the
    # target is 7c gross and 3.5c net, while a 6c stop is 9.5c net - 0.37:1. The
    # dashboard shows both numbers in dollars per position instead of enforcing a floor
    # that cannot be met. Raise this only with dips large enough to carry it (~30c).
    min_reward_risk: float = 0.0
    min_confidence: float = 0.5  # estimator confidence floor (0-1)
    min_price_cents: int = 5  # never buy below this (tail / fee-heavy)
    max_price_cents: int = 95  # never buy above this
    fee_rate: float = 0.07  # Kalshi taker fee coefficient

    # --- Liquidity -------------------------------------------------------
    max_spread_cents: int = 5  # > this flags [WIDE SPREAD / ILLIQUID]
    reject_wide_spread: bool = True  # PASS on wide spreads instead of trading them
    min_depth_contracts: int = 20  # contracts resting at best ask

    # --- Position sizing / exposure --------------------------------------
    kelly_fraction: float = 0.25
    # A position is at most `max` and, when there is not room for at least `min`,
    # is skipped rather than taken at a size too small to be worth the fees.
    min_position_dollars: float = 2.5
    max_position_dollars: float = 5.0
    max_total_exposure_dollars: float = 25.0
    max_open_positions: int = 5
    max_contracts_per_order: int = 100

    # --- Exit discipline -------------------------------------------------
    take_profit_cents: int = 10  # exit when bid >= entry + this
    stop_loss_cents: int = 8  # exit when bid <= entry - this
    hold_to_settlement_if_edge: bool = True  # keep winners when P_true still supports it

    # --- Circuit breakers -------------------------------------------------
    daily_loss_limit_dollars: float = 50.0
    max_consecutive_losses: int = 5

    # --- Estimators ------------------------------------------------------
    estimators: list[str] = field(default_factory=lambda: ["manual", "microstructure"])
    manual_estimates_path: str = "data/estimates.json"
    # Read from .env like everything else. It must be passed to the SDK explicitly:
    # loading a .env file does not put it in os.environ, which is where the SDK looks.
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    claude_effort: str = "high"

    # --- Bot mode ----------------------------------------------------------
    bot_mode: str = "tennis"  # "tennis" (contest swing trading) or "general" (EV on any market)
    bot_enabled: bool = False  # master switch; False = watch only, never enter. Turn on from the dashboard.

    # --- Which markets to trade ---------------------------------------------
    # Series tickers the bot scans and may trade. These four are the ones that have
    # been confirmed against the live exchange; everything else is switched on from
    # the dashboard after `discover` finds it, rather than guessed at here. A ticker
    # invented from memory looks identical to a market with nothing trading in it,
    # and that failure is silent, so this list only ever grows from real data.
    enabled_series: list[str] = field(default_factory=lambda: [
        "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
    ])
    # Optional prefix matching, for switching on a whole family at once (e.g. "KXNFL").
    series_prefixes: list[str] = field(default_factory=list)
    market_scan_limit: int = 1500
    # Pages of the whole exchange to sweep when discovering what is available. Each
    # page is 1000 markets; this runs on demand, not on the trading loop.
    discovery_pages: int = 12
    discovery_ttl_minutes: int = 60

    # --- Tennis: liveness ----------------------------------------------------
    live_only: bool = True  # only trade matches that are in progress
    live_window_minutes: int = 10  # price must have moved within this window to count as live
    live_min_move_cents: int = 1
    live_min_volume_delta: int = 1  # contracts traded inside the live window

    # --- Tennis: swing entry ---------------------------------------------------
    swing_window_minutes: int = 30  # rolling high is measured over this window
    dip_cents: int = 14  # buy only after the player's price fell this much from the rolling high
    min_form_score: float = 0.0  # 0-10; 0 = form informs fair value but never blocks a trade
    min_comeback_score: float = 0.0  # 0-10 how plausible a swing back is for this player
    min_pre_match_p_win: float = 0.15  # never buy a player the research gives less than this pre-match
    max_pre_match_price_cents: int = 85  # skip heavy favourites (little room for a swing)
    assessment_ttl_minutes: int = 180
    # Player form now comes from open data (no API key). Claude research is an
    # optional extra, off unless you turn it on.
    # Off until FORM_SOURCES names a dataset: there is no default source, and an
    # unconfigured fetch is twelve failed requests on every start.
    form_enabled: bool = False
    form_sources: list[str] = field(default_factory=list)  # URL templates containing {year}
    form_cache_hours: float = 12.0
    form_min_matches: int = 3
    research_enabled: bool = False  # Claude + web search; needs ANTHROPIC_API_KEY
    allow_without_research: bool = True  # trade the price action when no form data is found
    # Price-only entries with real money. The price rule assumes half of a dip
    # retraces; it cannot tell a tactical wobble from a player who just called the
    # trainer. On by default because that is the bot this was asked for - turn it
    # off (or run the Safe profile) to require form/research before live orders.
    allow_price_only_live: bool = True
    # Do not buy while the price is still making new lows - a cheap guard against
    # catching a knife that needs no data at all.
    require_stabilized: bool = True
    # One entry per market per match. The rolling high does not decay, so a player in a
    # sustained slide reads as a fresh dip at every new low: without this the bot buys
    # the same decline over and over, stopping out each time. Longer than a tennis
    # match, so in practice a market is traded once.
    reentry_cooldown_minutes: int = 180
    # Stop-losses cost double on Kalshi - the exit fee is charged on the loser too, and
    # a binary market's noise fills tight stops. Off means a position rides to its
    # target or to settlement, risking the whole stake but paying no exit fee on losers
    # and never being shaken out of a player who recovers.
    use_stop_loss: bool = True
    research_max_searches: int = 6

    # --- Tracking / dashboard --------------------------------------------
    db_path: str = "data/kalshitrader.db"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000
    log_level: str = "INFO"

    @property
    def base_url(self) -> str:
        if self.kalshi_base_url:
            return self.kalshi_base_url.rstrip("/")
        return PROD_BASE_URL if self.kalshi_env == "prod" else DEMO_BASE_URL

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def has_credentials(self) -> bool:
        return bool(self.kalshi_api_key_id and (self.kalshi_private_key_path or self.kalshi_private_key_pem))

    def validate(self) -> None:
        if self.trading_mode not in ("paper", "live"):
            raise ValueError("TRADING_MODE must be 'paper' or 'live'")
        if self.kalshi_env not in ("demo", "prod"):
            raise ValueError("KALSHI_ENV must be 'demo' or 'prod'")
        if self.bot_mode not in ("tennis", "general"):
            raise ValueError("BOT_MODE must be 'tennis' or 'general'")
        if self.is_live and not self.has_credentials:
            raise ValueError("live trading requires KALSHI_API_KEY_ID and a private key")
        if self.min_position_dollars > self.max_position_dollars:
            raise ValueError("MIN_POSITION_DOLLARS cannot exceed MAX_POSITION_DOLLARS")
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError("KELLY_FRACTION must be in (0, 1]")
        if self.min_price_cents < 1 or self.max_price_cents > 99 or self.min_price_cents >= self.max_price_cents:
            raise ValueError("price band must satisfy 1 <= MIN_PRICE_CENTS < MAX_PRICE_CENTS <= 99")

    def as_public_dict(self) -> dict:
        """Settings safe to show on the dashboard (no secrets)."""
        out = {}
        for f in fields(self):
            if "key" in f.name or "pem" in f.name:
                continue
            out[f.name] = getattr(self, f.name)
        out["base_url"] = self.base_url
        return out


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    global _VALUES
    values: dict[str, str] = dict(os.environ)
    if env_file and Path(env_file).exists():
        values.update({k: v for k, v in dotenv_values(env_file, encoding="utf-8").items() if v is not None})
    _VALUES = values
    s = Settings(
        kalshi_env=_env("KALSHI_ENV", "prod").lower(),
        kalshi_api_key_id=_env("KALSHI_API_KEY_ID"),
        kalshi_private_key_path=_env("KALSHI_PRIVATE_KEY_PATH"),
        kalshi_private_key_pem=_env("KALSHI_PRIVATE_KEY_PEM"),
        kalshi_base_url=_env("KALSHI_BASE_URL"),
        trading_mode=_env("TRADING_MODE", "paper").lower(),
        paper_starting_cash=_env_float("PAPER_STARTING_CASH", 1000.0),
        paper_slippage_cents=_env_int("PAPER_SLIPPAGE_CENTS", 1),
        scan_interval_seconds=max(15, _env_int("SCAN_INTERVAL_SECONDS", 60)),
        scan_limit=_env_int("SCAN_LIMIT", 200),
        series_tickers=_env_list("SERIES_TICKERS", []),
        event_tickers=_env_list("EVENT_TICKERS", []),
        min_hours_to_close=_env_float("MIN_HOURS_TO_CLOSE", 0.5),
        max_hours_to_close=_env_float("MAX_HOURS_TO_CLOSE", 24 * 14),
        min_volume_24h=_env_int("MIN_VOLUME_24H", 50),
        min_edge_cents=_env_float("MIN_EDGE_CENTS", 3.5),
        min_reward_risk=_env_float("MIN_REWARD_RISK", 0.0),
        min_confidence=_env_float("MIN_CONFIDENCE", 0.5),
        min_price_cents=_env_int("MIN_PRICE_CENTS", 5),
        max_price_cents=_env_int("MAX_PRICE_CENTS", 95),
        fee_rate=_env_float("FEE_RATE", 0.07),
        max_spread_cents=_env_int("MAX_SPREAD_CENTS", 5),
        reject_wide_spread=_env_bool("REJECT_WIDE_SPREAD", True),
        min_depth_contracts=_env_int("MIN_DEPTH_CONTRACTS", 20),
        kelly_fraction=_env_float("KELLY_FRACTION", 0.25),
        min_position_dollars=_env_float("MIN_POSITION_DOLLARS", 2.5),
        max_position_dollars=_env_float("MAX_POSITION_DOLLARS", 5.0),
        max_total_exposure_dollars=_env_float("MAX_TOTAL_EXPOSURE_DOLLARS", 25.0),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 5),
        max_contracts_per_order=_env_int("MAX_CONTRACTS_PER_ORDER", 100),
        take_profit_cents=_env_int("TAKE_PROFIT_CENTS", 10),
        stop_loss_cents=_env_int("STOP_LOSS_CENTS", 8),
        hold_to_settlement_if_edge=_env_bool("HOLD_TO_SETTLEMENT_IF_EDGE", True),
        daily_loss_limit_dollars=_env_float("DAILY_LOSS_LIMIT_DOLLARS", 50.0),
        max_consecutive_losses=_env_int("MAX_CONSECUTIVE_LOSSES", 5),
        estimators=_env_list("ESTIMATORS", ["manual", "microstructure"]),
        manual_estimates_path=resolve_state_path(_env("MANUAL_ESTIMATES_PATH", "data/estimates.json")),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5"),
        claude_effort=_env("CLAUDE_EFFORT", "high"),
        bot_mode=_env("BOT_MODE", "tennis").lower(),
        bot_enabled=_env_bool("BOT_ENABLED", False),
        enabled_series=_env_list("ENABLED_SERIES", Settings().enabled_series),
        series_prefixes=_env_list("SERIES_PREFIXES", []),
        market_scan_limit=_env_int("MARKET_SCAN_LIMIT", 1500),
        discovery_pages=_env_int("DISCOVERY_PAGES", 12),
        discovery_ttl_minutes=_env_int("DISCOVERY_TTL_MINUTES", 60),
        live_only=_env_bool("LIVE_ONLY", True),
        live_window_minutes=_env_int("LIVE_WINDOW_MINUTES", 10),
        live_min_move_cents=_env_int("LIVE_MIN_MOVE_CENTS", 1),
        live_min_volume_delta=_env_int("LIVE_MIN_VOLUME_DELTA", 1),
        swing_window_minutes=_env_int("SWING_WINDOW_MINUTES", 30),
        dip_cents=_env_int("DIP_CENTS", 14),
        min_form_score=_env_float("MIN_FORM_SCORE", 0.0),
        min_comeback_score=_env_float("MIN_COMEBACK_SCORE", 0.0),
        min_pre_match_p_win=_env_float("MIN_PRE_MATCH_P_WIN", 0.15),
        max_pre_match_price_cents=_env_int("MAX_PRE_MATCH_PRICE_CENTS", 85),
        assessment_ttl_minutes=_env_int("ASSESSMENT_TTL_MINUTES", 180),
        form_enabled=_env_bool("FORM_ENABLED", False),
        form_sources=_env_list("FORM_SOURCES", []),
        form_cache_hours=_env_float("FORM_CACHE_HOURS", 12.0),
        form_min_matches=_env_int("FORM_MIN_MATCHES", 3),
        research_enabled=_env_bool("RESEARCH_ENABLED", False),
        allow_without_research=_env_bool("ALLOW_WITHOUT_RESEARCH", True),
        allow_price_only_live=_env_bool("ALLOW_PRICE_ONLY_LIVE", True),
        require_stabilized=_env_bool("REQUIRE_STABILIZED", True),
        reentry_cooldown_minutes=_env_int("REENTRY_COOLDOWN_MINUTES", 180),
        use_stop_loss=_env_bool("USE_STOP_LOSS", True),
        research_max_searches=_env_int("RESEARCH_MAX_SEARCHES", 6),
        db_path=resolve_state_path(_env("DB_PATH", "data/kalshitrader.db")),
        dashboard_host=_env("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_env_int("DASHBOARD_PORT", 8000),
        log_level=_env("LOG_LEVEL", "INFO"),
    )
    s.validate()
    return s


# ---------------------------------------------------------------------------
# Runtime-tunable settings. The dashboard renders this list (label + help), the
# engine applies saved overrides at the start of every cycle, so changes take
# effect without a restart. `key` is the Settings attribute name.
# ---------------------------------------------------------------------------
TUNABLES: list[dict] = [
    {"group": "Bot", "key": "bot_enabled", "type": "bool", "label": "Bot enabled",
     "help": "Master switch. Off = the bot keeps watching and managing exits, but never opens a new position."},
    {"group": "Bot", "key": "live_only", "type": "bool", "label": "Live matches only",
     "help": "Only trade matches that are in progress (price has moved recently). Off also allows pre-match entries."},
    {"group": "Bot", "key": "form_enabled", "type": "bool", "label": "Use player form data",
     "help": "Pull recent results, surface record and head-to-head from a public tennis results dataset (no API key). Needs FORM_SOURCES set in .env to a CSV URL template - there is no default source. Informs fair value; the bot still trades price swings when a player is not found."},
    {"group": "Bot", "key": "research_enabled", "type": "bool", "label": "Research players with Claude",
     "help": "Before touching a match, ask Claude (with web search) for each player's recent form, surface fit, head-to-head and injury news. Needs an Anthropic key."},
    {"group": "Bot", "key": "allow_without_research", "type": "bool", "label": "Trade without research",
     "help": "If no research is available, fall back to price rules alone. Off (recommended) means no research = no trade."},
    {"group": "Bot", "key": "allow_price_only_live", "type": "bool", "label": "Price-only entries with real money",
     "help": "Let the bot buy dips in LIVE mode with no form or research behind them, valuing the dip at half a retrace. This is the aggressive setting: it cannot tell a tactical wobble from an injury, so some dips it buys will keep falling. Off means live orders need form or research; paper is unaffected."},
    {"group": "Bot", "key": "scan_interval_seconds", "type": "int", "label": "Scan every (seconds)", "min": 15, "max": 600,
     "help": "How often the bot re-reads prices, checks exits and looks for entries. Below ~15s Kalshi starts rate-limiting (429) and scans get dropped."},
    {"group": "Entry", "key": "live_min_volume_delta", "type": "int", "label": "Contracts traded to count as live", "min": 0, "max": 10000,
     "help": "A match counts as live only if this many contracts actually traded recently. Price alone is unreliable on Challenger events, where quotes drift hours before play."},

    {"group": "Entry", "key": "dip_cents", "type": "int", "label": "Buy after a dip of (¢)", "min": 1, "max": 50,
     "help": "A player is only bought after their price has fallen this many cents from its recent high. This is the swing you are buying into."},
    {"group": "Entry", "key": "reentry_cooldown_minutes", "type": "int", "label": "Wait before re-buying a player (min)", "min": 0, "max": 1440,
     "help": "After closing a position, leave that player alone this long. The dip is measured against a rolling high that does not decay, so a player sliding all match looks like a fresh dip at every new low - without this the bot buys the same decline again and again. 180 means once per match in practice; 0 turns it off."},
    {"group": "Exit", "key": "use_stop_loss", "type": "bool", "label": "Use a stop loss",
     "help": "On: sell when the price falls this far below entry. Off: ride the position to its target or to settlement - you risk the whole stake, but pay no exit fee on losers and are never shaken out of a player who recovers. On a binary market a tight stop is filled by noise as often as by a real move."},
    {"group": "Entry", "key": "require_stabilized", "type": "bool", "label": "Wait for the price to stop falling",
     "help": "Do not buy while the price is still making new lows. Cheap protection against catching a falling knife; turn off for the fastest entries."},
    {"group": "Exit", "key": "min_reward_risk", "type": "float", "label": "Minimum reward:risk (after fees)", "min": 0, "max": 10,
     "help": "Floor on reward:risk after Kalshi's two fees. 0 turns it off, which is the default. Fees come out of the win and are added to the loss, so a target that reads 1.1:1 in cents is nearer 0.5:1 in money - and no dip these profiles accept can reach 1:1 net. Raise this above 0 only alongside a much larger dip (about 30c), or the bot will pass on everything."},
    {"group": "Entry", "key": "min_edge_cents", "type": "float", "label": "Minimum edge (¢)", "min": 0, "max": 50,
     "help": "The research fair value must exceed the buy price by at least this much after fees."},
    {"group": "Entry", "key": "min_form_score", "type": "float", "label": "Minimum form score (0-10)", "min": 0, "max": 10,
     "help": "The analyst rates each player's recent form 0-10. A player below this is never bought, however cheap they get."},
    {"group": "Entry", "key": "min_comeback_score", "type": "float", "label": "Minimum comeback score (0-10)", "min": 0, "max": 10,
     "help": "How plausible the analyst thinks a swing back is for this player in this matchup (fitness, mentality, style vs opponent)."},
    {"group": "Entry", "key": "min_pre_match_p_win", "type": "float", "label": "Min pre-match win probability", "min": 0, "max": 1,
     "help": "Never buy a player the research gave less than this chance before the match started."},
    {"group": "Entry", "key": "max_pre_match_price_cents", "type": "int", "label": "Skip favourites above (¢)", "min": 50, "max": 99,
     "help": "Heavy favourites have little room to swing. Players priced above this pre-match are not traded."},
    {"group": "Entry", "key": "max_spread_cents", "type": "int", "label": "Max bid-ask spread (¢)", "min": 1, "max": 20,
     "help": "Wider spreads mean you lose money just entering and exiting. Markets wider than this are skipped."},
    {"group": "Entry", "key": "min_depth_contracts", "type": "int", "label": "Min contracts resting at the ask", "min": 0, "max": 100000,
     "help": "Skip a market with less than this size available to buy. Thin books mean you cannot get out at the price you expect."},
    {"group": "Entry", "key": "min_price_cents", "type": "int", "label": "Never buy below (¢)", "min": 1, "max": 98,
     "help": "Long shots rarely swing back and the fee eats the move. Below this price the bot passes."},
    {"group": "Entry", "key": "max_price_cents", "type": "int", "label": "Never buy above (¢)", "min": 2, "max": 99,
     "help": "A heavy favourite has little room left to rise, so there is nothing to sell into."},
    {"group": "Entry", "key": "min_confidence", "type": "float", "label": "Minimum research confidence", "min": 0, "max": 1,
     "help": "The analyst reports how sure it is (0-1). Below this, the assessment is treated as unknown."},

    {"group": "Exit", "key": "take_profit_cents", "type": "int", "label": "Sell at entry + (¢)", "min": 1, "max": 60,
     "help": "The sell target. Capped at the research fair value so you never wait for more than the player is worth."},
    {"group": "Exit", "key": "stop_loss_cents", "type": "int", "label": "Stop at entry − (¢)", "min": 1, "max": 60,
     "help": "If the price falls this far below your entry, the position is sold to cap the loss."},
    {"group": "Exit", "key": "hold_to_settlement_if_edge", "type": "bool", "label": "Hold winners while still +EV",
     "help": "When the sell target is hit but the research says the player is still underpriced, keep holding instead of selling."},

    {"group": "Risk", "key": "min_position_dollars", "type": "float", "label": "Min $ per position", "min": 0, "max": 10000,
     "help": "Skip a trade rather than take it below this size. Stops the bot nibbling positions too small to be worth the fees. Set 0 to allow any size."},
    {"group": "Risk", "key": "max_position_dollars", "type": "float", "label": "Max $ per position", "min": 1, "max": 10000,
     "help": "Most the bot will spend on one player in one match."},
    {"group": "Risk", "key": "max_total_exposure_dollars", "type": "float", "label": "Max $ in play at once", "min": 1, "max": 100000,
     "help": "Cap on the total cost of all open positions."},
    {"group": "Risk", "key": "max_open_positions", "type": "int", "label": "Max open positions", "min": 1, "max": 50,
     "help": "Number of positions the bot may hold at the same time."},
    {"group": "Risk", "key": "kelly_fraction", "type": "float", "label": "Kelly fraction", "min": 0.01, "max": 1,
     "help": "Sizing aggressiveness. 0.25 = quarter Kelly (conservative). 1 = full Kelly (very aggressive)."},
    {"group": "Risk", "key": "daily_loss_limit_dollars", "type": "float", "label": "Daily loss limit ($)", "min": 1, "max": 100000,
     "help": "Once today's realised losses reach this, no new positions are opened until tomorrow."},
    {"group": "Risk", "key": "max_consecutive_losses", "type": "int", "label": "Stop after N losses in a row", "min": 1, "max": 50,
     "help": "Circuit breaker: after this many losing trades in a row the bot stops entering until you resume it."},
]

TUNABLE_KEYS = {t["key"] for t in TUNABLES}


def coerce_override(key: str, value) -> object:
    spec = next(t for t in TUNABLES if t["key"] == key)
    if spec["type"] == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    num = int(value) if spec["type"] == "int" else float(value)
    if "min" in spec:
        num = max(spec["min"], num)
    if "max" in spec:
        num = min(spec["max"], num)
    return num


# Which markets to trade is a list, not a number or a switch, so it is saved and
# applied alongside the tunables rather than through them.
LIST_OVERRIDES = ("enabled_series", "series_prefixes")


def apply_overrides(settings: Settings, overrides: dict | None) -> Settings:
    """Mutate `settings` in place with dashboard overrides (unknown keys ignored)."""
    for key, value in (overrides or {}).items():
        if key in LIST_OVERRIDES:
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                setattr(settings, key, [v.strip().upper() for v in value if v.strip()])
            continue
        if key in TUNABLE_KEYS:
            try:
                setattr(settings, key, coerce_override(key, value))
            except (TypeError, ValueError):
                continue
    return settings


def update_env_file(path: str | Path, updates: dict[str, str | None]) -> None:
    """Set or replace KEY=value lines in a .env file, keeping everything else.

    Multi-line values (PEM keys) are stored double-quoted with escaped newlines,
    which python-dotenv reads back correctly. A value of None removes the key.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in updates:
            seen.add(key)
            if updates[key] is not None:
                out.append(_env_line(key, updates[key]))
            continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen and value is not None:
            out.append(_env_line(key, value))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _env_line(key: str, value: str) -> str:
    value = value.replace("\r\n", "\n").strip()
    if "\n" in value or " " in value or "#" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'{key}="{escaped}"'
    return f"{key}={value}"


# ---------------------------------------------------------------------------
# One-click profiles. Each is a full set of overrides for the entry and exit
# knobs; sizing is deliberately identical across all three so switching profile
# never changes how much money is at risk.
#
# The numbers are fee-aware, and that constrains them more than it looks. A
# round trip on Kalshi costs about 3.5c per contract at mid prices (7% x p x
# (1-p), charged on entry and again on exit). With no form data the bot values a
# dip at ask + dip/2, so the expected gross gain is dip/2 and the net edge is
# dip/2 - 3.5c. A 6c dip is therefore a losing trade by construction, however
# often it triggers; the smallest dip worth taking is around 8c, and a dip needs
# to be ~13c before a few cents of edge survive. Each profile's `dip_cents` is
# set from its own `min_edge_cents` on that basis.
# ---------------------------------------------------------------------------
PROFILES: dict[str, dict] = {
    "risky": {
        "label": "Risky",
        "blurb": "Smallest dips that still beat the fees, thin margins, wide stops, price-only entries allowed live. Most trades, least edge per trade. Sizes to $5 a position, $25 in play.",
        "settings": {
            "dip_cents": 10, "min_edge_cents": 1.5, "take_profit_cents": 6, "stop_loss_cents": 5, "min_reward_risk": 0.0,
            "max_spread_cents": 6, "min_depth_contracts": 5, "kelly_fraction": 0.5,
            "min_price_cents": 5, "max_price_cents": 95, "min_confidence": 0.0,
            "min_form_score": 0.0, "min_comeback_score": 0.0, "min_pre_match_p_win": 0.12,
            "require_stabilized": False, "max_open_positions": 5, "scan_interval_seconds": 15,
            "allow_price_only_live": True, "allow_without_research": True,
            "max_position_dollars": 5.0, "max_total_exposure_dollars": 25.0,
        },
    },
    "normal": {
        "label": "Normal",
        "blurb": "The balanced default: a real dip, a target that clears fees comfortably, sane liquidity. Sizes to $5 a position, $25 in play.",
        "settings": {
            "dip_cents": 14, "min_edge_cents": 3.5, "take_profit_cents": 8, "stop_loss_cents": 6, "min_reward_risk": 0.0,
            "max_spread_cents": 4, "min_depth_contracts": 20, "kelly_fraction": 0.25,
            "min_price_cents": 10, "max_price_cents": 90, "min_confidence": 0.0,
            "min_form_score": 0.0, "min_comeback_score": 0.0, "min_pre_match_p_win": 0.15,
            "require_stabilized": True, "max_open_positions": 5, "scan_interval_seconds": 20,
            "allow_price_only_live": True, "allow_without_research": True,
            "max_position_dollars": 5.0, "max_total_exposure_dollars": 25.0,
        },
    },
    "safe": {
        "label": "Safe",
        "blurb": "Near-arbitrage: only big dislocations on tight, deep books, and only players the data backs - no price-only entries. Needs a data source: with both player form and Claude research off, this profile passes on everything. Sizes to $5 a position, $25 in play.",
        "settings": {
            "dip_cents": 20, "min_edge_cents": 7.0, "take_profit_cents": 12, "stop_loss_cents": 6, "min_reward_risk": 0.0,
            "max_spread_cents": 2, "min_depth_contracts": 50, "kelly_fraction": 0.15,
            "min_price_cents": 20, "max_price_cents": 80, "min_confidence": 0.3,
            "min_form_score": 5.0, "min_comeback_score": 5.0, "min_pre_match_p_win": 0.35,
            "require_stabilized": True, "max_open_positions": 3, "scan_interval_seconds": 30,
            "allow_price_only_live": False, "allow_without_research": False,
            "max_position_dollars": 5.0, "max_total_exposure_dollars": 25.0,
        },
    },
}


def profile_overrides(name: str) -> dict:
    """The override map for a profile, validated against the tunable list."""
    profile = PROFILES.get(name.lower())
    if profile is None:
        raise ValueError(f"unknown profile {name!r}; choose from {', '.join(PROFILES)}")
    unknown = [k for k in profile["settings"] if k not in TUNABLE_KEYS]
    if unknown:  # a profile that names a setting the dashboard cannot show is a bug
        raise ValueError(f"profile {name} sets non-tunable keys: {unknown}")
    return dict(profile["settings"])
