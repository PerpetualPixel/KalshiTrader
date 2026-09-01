from src.strategies.base import market_prices
from src.strategies.swing_trader import (
    LOOKBACK_SECONDS,
    exit_reason,
    swing_drop,
)


def test_market_prices_reads_list_fields():
    m = {"yes_bid": 55, "yes_ask": 58, "no_bid": 42, "no_ask": 45}
    assert market_prices(m) == {"yes_bid": 55, "yes_ask": 58, "no_bid": 42, "no_ask": 45}


def test_market_prices_derives_no_side_from_yes():
    m = {"yes_bid": 55, "yes_ask": 58}
    p = market_prices(m)
    assert p["no_bid"] == 42  # 100 - yes_ask
    assert p["no_ask"] == 45  # 100 - yes_bid


def test_market_prices_treats_zero_as_missing():
    # Kalshi reports 0 on an empty book side
    m = {"yes_bid": 0, "yes_ask": 58}
    p = market_prices(m)
    assert p["yes_bid"] is None
    assert p["no_ask"] is None  # can't derive from missing yes_bid
    assert p["no_bid"] == 42


def test_swing_drop_measures_fall_from_recent_high():
    now = 1000.0
    history = [(now - 200, 60), (now - 100, 55), (now - 50, 52)]
    assert swing_drop(history, now, 48) == 12  # 60 -> 48


def test_swing_drop_ignores_prices_outside_lookback():
    now = 10_000.0
    history = [(now - LOOKBACK_SECONDS - 60, 90), (now - 60, 55)]
    assert swing_drop(history, now, 50) == 5  # the 90 is stale


def test_swing_drop_empty_history_is_zero():
    assert swing_drop([], 1000.0, 50) == 0


def test_exit_take_profit():
    assert exit_reason(50, 56, held_seconds=60, take_profit_cents=5,
                       stop_loss_cents=8, max_hold_seconds=1800) == "take_profit"


def test_exit_stop_loss():
    assert exit_reason(50, 42, held_seconds=60, take_profit_cents=5,
                       stop_loss_cents=8, max_hold_seconds=1800) == "stop_loss"


def test_exit_max_hold():
    assert exit_reason(50, 51, held_seconds=1801, take_profit_cents=5,
                       stop_loss_cents=8, max_hold_seconds=1800) == "max_hold"


def test_no_exit_inside_bands():
    assert exit_reason(50, 52, held_seconds=60, take_profit_cents=5,
                       stop_loss_cents=8, max_hold_seconds=1800) is None


# ── Settings plumbing ─────────────────────────────────────────────────
# The swing trader spent weeks placing no trades because SettingsPatch was
# missing every swing_* field: pydantic dropped them silently, so the
# dashboard's series and thresholds never reached the engine.


def test_settings_patch_accepts_every_swing_field():
    from src.config import BotSettings
    from src.main import SettingsPatch

    body = {
        "swing_series": "KXATPMATCH,KXWTAMATCH",
        "swing_drop_cents": 5,
        "swing_lookback_seconds": 420,
        "swing_max_spread_cents": 8,
        "swing_price_band_low": 10,
        "swing_price_band_high": 90,
        "swing_take_profit_cents": 4,
        "swing_stop_loss_cents": 7,
        "swing_max_hold_minutes": 20,
        "swing_max_positions": 4,
    }
    patch = SettingsPatch(**body).model_dump(exclude_none=True)
    patch.pop("confirm_live", None)
    assert patch == body
    settings = BotSettings().update(patch)
    assert settings.swing_series == "KXATPMATCH,KXWTAMATCH"
    assert settings.swing_max_spread_cents == 8
    assert settings.swing_price_band_low == 10


def test_settings_patch_rejects_unknown_field():
    import pytest
    from pydantic import ValidationError

    from src.main import SettingsPatch

    with pytest.raises(ValidationError):
        SettingsPatch(swing_drop_cnts=5)  # typo must not be swallowed


def test_price_band_must_be_ordered():
    import pytest
    from pydantic import ValidationError

    from src.config import BotSettings

    with pytest.raises(ValidationError):
        BotSettings(swing_price_band_low=80, swing_price_band_high=20)


def test_swing_drop_honours_a_custom_lookback():
    now = 1000.0
    history = [(now - 400, 70), (now - 100, 60)]
    assert swing_drop(history, now, 55, lookback_seconds=300) == 5
    assert swing_drop(history, now, 55, lookback_seconds=600) == 15


# ── Entry-slot accounting ─────────────────────────────────────────────


def _intent(ticker="TEST", side="yes", action="buy"):
    from src.strategies.base import OrderIntent

    return OrderIntent(ticker, side, action, 1, 50, "swing dip -5c")


def test_rejected_order_releases_the_entry_slot():
    """A risk-blocked or rejected order must not hold a position slot —
    otherwise a few rejections silently retire the strategy."""
    from src.strategies.swing_trader import SwingTraderStrategy

    s = SwingTraderStrategy()
    s.entries[("TEST", "yes")] = {"price": 50, "count": 1, "ts": 0.0, "confirmed": False}
    s.on_order_result(_intent(), ok=False)
    assert s.entries == {}


def test_accepted_order_confirms_the_entry():
    from src.strategies.swing_trader import SwingTraderStrategy

    s = SwingTraderStrategy()
    s.entries[("TEST", "yes")] = {"price": 50, "count": 1, "ts": 0.0, "confirmed": False}
    s.on_order_result(_intent(), ok=True)
    assert s.entries[("TEST", "yes")]["confirmed"] is True


def test_exit_order_result_leaves_entries_alone():
    from src.strategies.swing_trader import SwingTraderStrategy

    s = SwingTraderStrategy()
    entry = {"price": 50, "count": 1, "ts": 0.0, "confirmed": True}
    s.entries[("TEST", "yes")] = entry
    s.on_order_result(_intent(action="sell"), ok=False)
    assert s.entries[("TEST", "yes")] is entry


# ── Full scan behaviour ───────────────────────────────────────────────


class _FakeClient:
    """Serves one market from /markets and no open positions."""

    def __init__(self, market):
        self.market = market

    async def get_markets(self, **kwargs):
        if kwargs.get("tickers"):
            return {"markets": [], "cursor": None}
        return {"markets": [self.market], "cursor": None}

    async def get_positions(self):
        return {"market_positions": []}


def _ctx(client, **overrides):
    from src.config import BotSettings
    from src.strategies.base import StrategyContext

    async def log(message, level="info"):
        return None

    settings = BotSettings(swing_series="KXTEST", **overrides)
    return StrategyContext(client=client, settings=settings, log=log)


async def _scan(strategy, ctx):
    return await strategy.scan_once(ctx)


def test_scan_buys_after_the_ask_drops_through_the_threshold():
    import asyncio

    from src.strategies.swing_trader import SwingTraderStrategy

    market = {"ticker": "KXTEST-A", "yes_bid": 58, "yes_ask": 60}
    client = _FakeClient(market)
    strategy = SwingTraderStrategy()
    ctx = _ctx(client, swing_drop_cents=5)

    # First scan only records the ask; there is no history to fall from.
    assert asyncio.run(_scan(strategy, ctx)) == []

    market["yes_bid"], market["yes_ask"] = 53, 55  # a 5c drop on the YES ask
    intents = asyncio.run(_scan(strategy, ctx))
    buys = [i for i in intents if i.action == "buy" and i.side == "yes"]
    assert len(buys) == 1
    assert buys[0].ticker == "KXTEST-A"
    assert buys[0].price_cents == 55


def test_wide_spread_blocks_the_entry_and_is_reported():
    import asyncio

    from src.strategies.swing_trader import SwingTraderStrategy

    market = {"ticker": "KXTEST-A", "yes_bid": 50, "yes_ask": 60}
    client = _FakeClient(market)
    strategy = SwingTraderStrategy()
    ctx = _ctx(client, swing_drop_cents=5, swing_max_spread_cents=8)

    asyncio.run(_scan(strategy, ctx))
    market["yes_bid"], market["yes_ask"] = 45, 55  # 5c drop, but a 10c spread
    assert asyncio.run(_scan(strategy, ctx)) == []
    assert strategy._last_scan["skips"].get("spread")


def test_price_band_blocks_the_entry():
    import asyncio

    from src.strategies.swing_trader import SwingTraderStrategy

    market = {"ticker": "KXTEST-A", "yes_bid": 10, "yes_ask": 12}
    client = _FakeClient(market)
    strategy = SwingTraderStrategy()
    ctx = _ctx(client, swing_drop_cents=5, swing_price_band_low=15)

    asyncio.run(_scan(strategy, ctx))
    market["yes_bid"], market["yes_ask"] = 5, 7
    assert asyncio.run(_scan(strategy, ctx)) == []
    assert strategy._last_scan["skips"].get("band")


def test_scan_records_the_closest_candidate_when_nothing_fires():
    import asyncio

    from src.strategies.swing_trader import SwingTraderStrategy

    market = {"ticker": "KXTEST-A", "yes_bid": 58, "yes_ask": 60}
    client = _FakeClient(market)
    strategy = SwingTraderStrategy()
    ctx = _ctx(client, swing_drop_cents=9)

    asyncio.run(_scan(strategy, ctx))
    market["yes_bid"], market["yes_ask"] = 55, 57  # only a 3c drop
    asyncio.run(_scan(strategy, ctx))
    assert strategy._last_scan["best_candidate_drop_cents"] == 3
    assert strategy._last_scan["skips"].get("drop")
