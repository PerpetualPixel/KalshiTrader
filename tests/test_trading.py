"""Tennis: market parsing, research gate, swing entries, and full paper cycles."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from kalshitrader.config import Settings
from kalshitrader.execution.paper import PaperBroker
from kalshitrader.kalshi.client import KalshiClient
from kalshitrader.kalshi.models import Market
from kalshitrader.markets.contest import Contest, find_contests, parse_sides
from kalshitrader.tennis.analyst import MatchAssessment, TennisAnalyst
from kalshitrader.tracking.db import Store
from kalshitrader.trading.engine import TennisEngine
from tests.conftest import FakeExchange, make_market

# ------------------------------------------------------------------ fixtures

def tennis_market(ticker, event, title, subtitle="", yes_bid=48, yes_ask=50, volume=800, series="KXATPMATCH"):
    m = make_market(ticker, yes_bid=yes_bid, yes_ask=yes_ask, volume_24h=volume, hours_to_close=6, title=title)
    m.update(event_ticker=event, series_ticker=series, yes_sub_title=subtitle)
    return m


def assessment(p_a=0.60, form_a=8, form_b=5, comeback_a=8, comeback_b=4, conf=0.75, fitness_a=1):
    return MatchAssessment(
        player_a="Carlos Alcaraz", player_b="Jannik Sinner", tournament="US Open", surface="hard", p_a_wins=p_a,
        a={"name": "Carlos Alcaraz", "form_score": form_a, "comeback_score": comeback_a, "surface_fit": 8, "fitness_risk": fitness_a, "key_facts": ["won last 5"]},
        b={"name": "Jannik Sinner", "form_score": form_b, "comeback_score": comeback_b, "surface_fit": 7, "fitness_risk": 2, "key_facts": []},
        confidence=conf, trade_view="Alcaraz is the buy on any dip.", sources=["https://example.org"],
    )


class StubAnalyst:
    """Stands in for Claude: returns a fixed assessment (or None) and counts calls."""

    def __init__(self, store, result):
        self.store, self.result, self.calls, self.enabled = store, result, 0, True
        self.ttl, self.max_searches, self.model = 180, 6, "stub"
        self.api_key = None

    status = "stub"

    def reset_errors(self):
        pass

    def request(self, match):
        # The real analyst queues this; the stub is instant, so resolve inline.
        return self.assess(match)

    pending = 0

    def cached(self, match):
        payload = self.store.get_assessment(match.key)
        return MatchAssessment.model_validate(payload) if payload else None

    def assess(self, match):
        self.calls += 1
        if self.result is None:
            return None
        self.store.put_assessment(match.key, self.result.model_dump(), self.ttl)
        return self.result


_n = [0]


def tennis_settings(tmp_path, **kw) -> Settings:
    _n[0] += 1
    base = dict(db_path=str(tmp_path / f"t{_n[0]}.db"), bot_mode="tennis", min_volume_24h=10, min_depth_contracts=5, paper_slippage_cents=0,
                dip_cents=8, live_only=True, live_window_minutes=10, swing_window_minutes=60, min_edge_cents=4.0, min_confidence=0.5,
                bot_enabled=True, max_position_dollars=25.0,
                # These tests drive the Claude research path and the form gates directly,
                # so they opt in rather than inheriting the shipped price-first defaults.
                research_enabled=True, form_enabled=False, allow_without_research=False,
                min_form_score=6.0, min_comeback_score=5.0, min_pre_match_p_win=0.35,
                max_total_exposure_dollars=200.0, min_position_dollars=0.0)
    base.update(kw)
    s = Settings(**base)
    s.validate()
    return s


def build(tmp_path, exchange, result, **kw):
    s = tennis_settings(tmp_path, **kw)
    client = KalshiClient("https://fake.kalshi.test/trade-api/v2", transport=httpx.MockTransport(exchange.handler))
    client._backoff = lambda a: None
    store = Store(s.db_path)
    eng = TennisEngine(s, client, store, PaperBroker(store, 1000.0, 0), analyst=StubAnalyst(store, result), env_file=None)
    return eng, store, s


def two_market_exchange():
    ex = FakeExchange()
    ex.add(tennis_market("KXATPMATCH-25SEP07ALCSIN-ALC", "KXATPMATCH-25SEP07ALCSIN", "Alcaraz vs Sinner Winner?", "Carlos Alcaraz", 58, 60))
    ex.add(tennis_market("KXATPMATCH-25SEP07ALCSIN-SIN", "KXATPMATCH-25SEP07ALCSIN", "Alcaraz vs Sinner Winner?", "Jannik Sinner", 39, 41))
    ex.add(make_market("KXBTC-1", title="Bitcoin above 60k?"))  # not tennis
    return ex


# ------------------------------------------------------------------- parsing

def test_parse_players_variants():
    assert parse_sides("Carlos Alcaraz vs. Jannik Sinner") == ("Carlos Alcaraz", "Jannik Sinner")
    assert parse_sides("Will Iga Swiatek vs Coco Gauff?") == ("Iga Swiatek", "Coco Gauff")
    assert parse_sides("Djokovic v Zverev - US Open R4") == ("Djokovic", "Zverev")
    assert parse_sides("Bitcoin above 60k?") is None


def test_find_matches_two_market_shape():
    ms = [Market.from_api(m) for m in two_market_exchange().markets.values()]
    matches = find_contests(ms, ["KXATPMATCH"])
    assert len(matches) == 1
    m = matches[0]
    assert {m.player_a, m.player_b} == {"Carlos Alcaraz", "Jannik Sinner"}
    assert m.leg("Carlos Alcaraz").side == "yes" and m.leg("Carlos Alcaraz").ticker.endswith("-ALC")
    assert m.leg("Jannik Sinner").side == "yes" and m.leg("Jannik Sinner").ask == 41
    assert m.tournament and m.close_time is not None


def test_find_matches_single_market_shape():
    """One "A vs B" market becomes a yes leg and a no leg on the same ticker, and only
    the series that were switched on are scanned."""
    ms = [Market.from_api(tennis_market("KXWTAMATCH-1", "KXWTAMATCH-1", "Swiatek vs Gauff", series="KXWTAMATCH")),
          Market.from_api({**make_market("OTHER-1", title="Rybakina vs Sabalenka - WTA Cincinnati"), "series_ticker": "OTHER"})]
    enabled = find_contests(ms, ["KXWTAMATCH"])
    assert [m.title for m in enabled] == ["Swiatek vs Gauff"]
    assert enabled[0].leg("Gauff").side == "no"
    assert find_contests(ms, []) == [], "nothing enabled means nothing is scanned"
    assert {m.title for m in find_contests(ms, ["KXWTAMATCH", "OTHER"])} == {"Swiatek vs Gauff", "Rybakina vs Sabalenka"}


# ---------------------------------------------------------------- strategy

def run_cycles_with_dip(eng, ex, ticker, path):
    """Drive the exchange through a price path, one cycle per step, returning the results."""
    out = []
    for bid in path:
        ex.set_price(ticker, bid, bid + 2)
        out.append(eng.cycle())
    return out


def test_no_research_means_no_trade_when_forbidden(tmp_path):
    """With `allow_without_research` off, an unresearched match is never traded."""
    ex = two_market_exchange()
    eng, store, _ = build(tmp_path, ex, None, allow_without_research=False)
    res = run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 55, 50, 46])
    assert all(r.trades_opened == 0 for r in res)
    reasons = {sig.rationale for r in res for sig in r.signals}
    assert any("no research" in r for r in reasons)


def test_buys_backed_player_on_live_dip(tmp_path):
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.65))
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    # price ticks (live) but only a small dip: PASS with the dip reason
    res = run_cycles_with_dip(eng, ex, alc, [58, 57, 56])
    assert all(r.trades_opened == 0 for r in res)
    assert any("dip" in sig.rationale for sig in res[-1].signals)
    # now the dip: 58 -> 46 bid (ask 48). fair 0.65 -> ev_net ~15c
    res = run_cycles_with_dip(eng, ex, alc, [46])
    assert res[0].trades_opened == 1
    t = store.open_trades()[0]
    assert t["ticker"] == alc and t["side"] == "yes" and t["entry_price"] == 48
    assert t["take_profit"] == 58 and t["stop_loss"] == 40  # capped at entry+10, fair=65 not binding
    assert "Alcaraz" in t["title"]
    # research was called once and then served from cache
    assert eng.analyst.calls == 1
    # watch state exposes the match for the dashboard
    watch = store.get_state("tennis_watch")
    assert watch[0]["live"] is True and watch[0]["researched"] is True
    # rebound: at bid 58 the target is hit but fair 65 still leaves >4c edge -> hold; at 62 it sells
    res = run_cycles_with_dip(eng, ex, alc, [52, 58])
    assert res[-1].trades_closed == 0 and len(store.open_trades()) == 1
    res = run_cycles_with_dip(eng, ex, alc, [62])
    assert res[-1].trades_closed == 1
    closed = store.closed_trades()[0]
    assert closed["exit_reason"] == "take-profit" and closed["pnl"] > 0


def test_sell_target_capped_at_fair_value_but_never_below_fees(tmp_path):
    """The target is capped at fair value, and floored so it always clears the round trip."""
    from kalshitrader.analysis.ev import kalshi_fee_cents

    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.55))
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    run_cycles_with_dip(eng, ex, alc, [58, 57, 44])
    t = store.open_trades()[0]
    assert t["entry_price"] == 46 and t["take_profit"] == 55  # min(46+10, fair 55)
    floor = 46 + int(kalshi_fee_cents(46, s.fee_rate) * 2) + 1
    assert t["take_profit"] >= floor, "target must clear entry + exit fees"


def test_target_is_lifted_above_the_round_trip_fee(tmp_path):
    """A tiny take-profit would book a loss after fees, so it is raised to clear them."""
    from kalshitrader.analysis.ev import kalshi_fee_cents

    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.75), take_profit_cents=1)
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    run_cycles_with_dip(eng, ex, alc, [58, 57, 46])
    t = store.open_trades()[0]
    round_trip = kalshi_fee_cents(t["entry_price"], s.fee_rate) * 2
    assert t["take_profit"] - t["entry_price"] > round_trip


def test_form_gate_blocks_out_of_form_player(tmp_path):
    ex = two_market_exchange()
    eng, store, _ = build(tmp_path, ex, assessment(p_a=0.65, form_a=4))
    res = run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 46])
    assert res[-1].trades_opened == 0
    assert any("form 4.0" in sig.rationale for sig in res[-1].signals)


def test_comeback_and_fitness_gates(tmp_path):
    ex = two_market_exchange()
    eng, _, _ = build(tmp_path, ex, assessment(p_a=0.65, comeback_a=3))
    res = run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 46])
    assert any("comeback 3.0" in sig.rationale for sig in res[-1].signals)
    ex = two_market_exchange()
    eng, _, _ = build(tmp_path, ex, assessment(p_a=0.65, fitness_a=9))
    res = run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 46])
    assert any("fitness risk" in sig.rationale for sig in res[-1].signals)


def test_live_only_blocks_static_prices(tmp_path):
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.65))
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    # seed history with an old high, then hold a low price still (no movement in the live window)
    old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
    with store.tx() as conn:
        conn.execute("INSERT INTO market_snapshots(ts, ticker, yes_bid, yes_ask, no_bid, no_ask, last_price, volume_24h, open_interest) VALUES(?,?,?,?,?,?,?,?,?)",
                     (old, alc, 60, 62, 38, 40, 60, 800, 0))
    ex.set_price(alc, 46, 48)
    res = eng.cycle()  # first observation after the gap: no movement inside the last 10 minutes
    assert res.trades_opened == 0
    assert any("not live" in sig.rationale for sig in res.signals)
    assert eng.analyst.calls == 0  # no research spent on a market that is not live
    s.live_only = False
    assert eng.cycle().trades_opened == 1  # rolling high 62c (old snapshot) vs ask 48c -> 14c dip


def test_shipped_defaults_are_safe():
    """Out of the box the bot must not trade, and must risk at most $1 per position."""
    d = Settings()
    assert d.bot_enabled is False  # the risk caps themselves are pinned in test_risk.py
    assert d.kalshi_env == "prod" and d.trading_mode == "paper"


def test_bot_disabled_toggle_and_overrides(tmp_path):
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.65))
    store.set_state("settings_overrides", {"bot_enabled": False, "dip_cents": 20})
    res = run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 46])
    assert res[-1].trades_opened == 0 and s.bot_enabled is False and s.dip_cents == 20
    store.set_state("settings_overrides", {"bot_enabled": True, "dip_cents": 8})
    assert eng.cycle().trades_opened == 1


def test_trade_without_research_uses_price_rules(tmp_path):
    """With research off, fair value = ask + half the dip, so a big dip is buyable."""
    ex = two_market_exchange()
    eng, store, _ = build(tmp_path, ex, None, allow_without_research=True)
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    # small dip: 60c high -> 56c ask, fair 58c, edge under the 4c floor -> PASS
    res = run_cycles_with_dip(eng, ex, alc, [58, 57, 54])
    assert res[-1].trades_opened == 0
    assert any("edge" in sig.rationale or "dip" in sig.rationale for sig in res[-1].signals)
    # big dip: 60c high -> 42c ask, fair 51c, edge ~+7c -> BUY, sell target capped at fair
    res = run_cycles_with_dip(eng, ex, alc, [40])
    assert res[-1].trades_opened == 1
    t = store.open_trades()[0]
    assert t["entry_price"] == 42 and t["take_profit"] == 51 and t["stop_loss"] == 34
    sig = next(s for s in res[-1].signals if s.is_trade)
    assert sig.estimator == "price" and "price-only fair value" in sig.rationale


def test_settlement_closes_tennis_position(tmp_path):
    ex = two_market_exchange()
    eng, store, _ = build(tmp_path, ex, assessment(p_a=0.65))
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    run_cycles_with_dip(eng, ex, alc, [58, 57, 46])
    ex.settle(alc, "yes")
    ex.settle("KXATPMATCH-25SEP07ALCSIN-SIN", "no")
    assert eng.cycle().trades_closed == 1
    assert store.closed_trades()[0]["exit_price"] == 100


def test_series_fetch_uses_configured_tickers(tmp_path):
    ex = two_market_exchange()
    eng, _, s = build(tmp_path, ex, None)
    markets = eng.fetch_markets()
    assert {m.series_ticker for m in markets} == {"KXATPMATCH"}
    requested = {r.url.params.get("series_ticker") for r in ex.requests if r.url.path.endswith("/markets")}
    assert requested == set(s.enabled_series)


# ---------------------------------------------------------------- analyst

def test_analyst_two_step_calls_and_cache(tmp_path):
    store = Store(":memory:")
    brief = "Alcaraz won 5 straight on hard courts; Sinner returning from injury."
    structured = assessment().model_dump_json()
    responses = [
        SimpleNamespace(stop_reason="pause_turn", content=[SimpleNamespace(type="text", text="searching...")]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=brief)]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=structured)]),
    ]
    calls = []

    def create(**kw):
        calls.append(kw)
        return responses.pop(0)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    analyst = TennisAnalyst(store, client=client, model="claude-opus-5", max_searches=3)
    ms = [Market.from_api(m) for m in two_market_exchange().markets.values()]
    match = find_contests(ms, ["KXATPMATCH"])[0]
    a = analyst.assess(match)
    assert a is not None and a.p_a_wins == 0.60
    assert len(calls) == 3
    assert calls[0]["tools"][0]["type"] == "web_search_20260209" and calls[0]["tools"][0]["max_uses"] == 3
    assert calls[1]["messages"][-1]["role"] == "assistant"  # pause_turn continuation
    assert calls[2]["output_config"]["format"]["type"] == "json_schema" and "tools" not in calls[2]
    assert analyst.assess(match).p_a_wins == 0.60 and len(calls) == 3  # cached
    assert analyst.status == "ready"


def test_analyst_unavailable_without_package(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "anthropic":
            raise ImportError("no anthropic")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    analyst = TennisAnalyst(Store(":memory:"))
    assert not analyst.available and "not installed" in analyst.status
    ms = [Market.from_api(m) for m in two_market_exchange().markets.values()]
    assert analyst.assess(find_contests(ms, ["KXATPMATCH"])[0]) is None


def test_discovery_diagnostics_record_missing_series(tmp_path):
    ex = two_market_exchange()
    original = ex.handler

    def handler(request):
        if request.url.params.get("series_ticker") == "KXWTAMATCH":
            return httpx.Response(404, json={"error": "series not found"})
        return original(request)

    eng, store, s = build(tmp_path, ex, None)
    eng.client._http._transport.handler = handler
    eng.cycle()
    d = store.get_state("tennis_discovery")
    assert d["KXATPMATCH"] == "2 open markets" and d["KXWTAMATCH"].startswith("error 404")


def test_mode_switch_via_env_file(tmp_path, rsa_pem):
    """Flipping TRADING_MODE in .env (as the dashboard does) swaps the broker on the next cycle."""
    env = tmp_path / ".env"
    env.write_text(f"DB_PATH={tmp_path / 'm.db'}\nTRADING_MODE=paper\nKALSHI_API_KEY_ID=k\n")
    ex = two_market_exchange()
    from kalshitrader.config import load_settings, update_env_file

    s = load_settings(env)
    client = KalshiClient("https://fake.kalshi.test/trade-api/v2", api_key_id="k", private_key_pem=rsa_pem, transport=httpx.MockTransport(ex.handler))
    client._backoff = lambda a: None
    store = Store(s.db_path)
    eng = TennisEngine(s, client, store, PaperBroker(store, 1000.0, 0), analyst=StubAnalyst(store, None), env_file=str(env))
    assert eng.mode == "paper"
    import os
    import time
    time.sleep(0.01)
    update_env_file(env, {"TRADING_MODE": "live", "KALSHI_PRIVATE_KEY_PEM": rsa_pem})
    os.utime(env, None)
    eng.refresh_settings()
    assert eng.mode == "live" and eng.broker.__class__.__name__ == "LiveBroker"
    update_env_file(env, {"TRADING_MODE": "paper"})
    os.utime(env, (time.time() + 5, time.time() + 5))
    eng.refresh_settings()
    assert eng.mode == "paper"


def active_status_exchange(zero_prices=False):
    """Kalshi reports in-play markets as status='active', and the /markets list
    response can carry no quotes at all (0/0) while the order book is fine."""
    ex = FakeExchange()
    for tick, sub, bid in (("KXATPCHALLENGERMATCH-SAKNAM-SAK", "Rei Sakamoto", 32),
                           ("KXATPCHALLENGERMATCH-SAKNAM-NAM", "Ji Sung Nam", 67)):
        m = tennis_market(tick, "KXATPCHALLENGERMATCH-SAKNAM", "Sakamoto vs Nam Winner?", sub,
                          bid, bid + 1, series="KXATPCHALLENGERMATCH")
        m["status"] = "active"
        ex.add(m)
        if zero_prices:
            ex.hidden_quotes.add(tick)
    ex.depth = 400
    return ex


def test_active_status_is_tradeable(tmp_path):
    """A live match reported as status='active' must not be shown as closed."""
    ex = active_status_exchange()
    eng, store, _ = build(tmp_path, ex, None, enabled_series=["KXATPCHALLENGERMATCH"])
    eng.cycle()
    row = store.get_state("tennis_watch")[0]
    assert row["status"] != "closed"
    assert row["legs"][0]["ask"] > 0


def test_prices_come_from_orderbook_when_list_has_none(tmp_path):
    """0c/0c in the list response must be filled in from the book, not shown as 0."""
    ex = active_status_exchange(zero_prices=True)
    eng, store, s = build(tmp_path, ex, None, enabled_series=["KXATPCHALLENGERMATCH"])
    eng.cycle()
    row = store.get_state("tennis_watch")[0]
    sak = next(leg for leg in row["legs"] if leg["player"] == "Rei Sakamoto")
    assert sak["bid"] == 32 and sak["ask"] == 33, row["legs"]
    # and the snapshot history carries the real quotes so dips can be measured
    snap = store.snapshots("KXATPCHALLENGERMATCH-SAKNAM-SAK", limit=1)[-1]
    assert snap["yes_bid"] == 32 and snap["yes_ask"] == 33


def test_active_market_with_book_prices_can_trade(tmp_path):
    """End to end on the real-world shape: active status + book-only prices -> a buy."""
    ex = active_status_exchange(zero_prices=True)
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.70), enabled_series=["KXATPCHALLENGERMATCH"])
    eng.analyst.result.player_a, eng.analyst.result.a.name = "Rei Sakamoto", "Rei Sakamoto"
    eng.analyst.result.player_b, eng.analyst.result.b.name = "Ji Sung Nam", "Ji Sung Nam"
    sak = "KXATPCHALLENGERMATCH-SAKNAM-SAK"
    eng.cycle()
    ex.set_price(sak, 30, 31)
    eng.cycle()
    ex.set_price(sak, 20, 21)  # a 12c dip from the 33c high
    res = eng.cycle()
    assert res.trades_opened == 1, [sig.rationale for sig in res.signals]
    assert store.open_trades()[0]["entry_price"] == 21


def test_price_only_live_entries_can_be_switched_off(tmp_path):
    """`allow_price_only_live` is the gate between a dip with no data behind it and a
    real order. Off, live money is untouched; paper is unaffected either way."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    s.trading_mode = "live"
    s.allow_price_only_live = False
    res = run_cycles_with_dip(eng, ex, alc, [58, 57, 40])
    assert res[-1].trades_opened == 0
    assert any("price-only entries are off for live trading" in sig.rationale for sig in res[-1].signals)
    s.trading_mode = "paper"  # same dip, paper money: the rule is allowed to act
    assert eng.cycle().trades_opened == 1


def test_price_only_live_entries_are_on_by_default(tmp_path):
    """The default build trades a live dip with no research behind it - that is the
    bot that was asked for, and the setting above is how you turn it off."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    assert s.allow_price_only_live is True
    s.trading_mode = "live"
    res = run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 40])
    assert res[-1].trades_opened == 1, [sig.rationale for sig in res[-1].signals]


def test_liveness_needs_real_volume_not_just_drifting_quotes(tmp_path):
    """A thin Challenger book can drift hours before play. Quotes alone are not 'live'."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.65), live_min_volume_delta=10)
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    eng.cycle()
    # price drifts down with no trades at all: not live, so no entry
    for bid in (57, 50, 46):
        ex.set_price(alc, bid, bid + 2, traded=0)
        res = eng.cycle()
    assert res.trades_opened == 0
    assert any("not live" in sig.rationale for sig in res.signals)
    # now the tape moves: the same book is live and the dip is tradeable
    ex.set_price(alc, 46, 48, traded=50)
    res = eng.cycle()
    assert res.trades_opened == 1


def test_reconcile_closes_settled_position_missing_from_exchange(tmp_path, rsa_pem):
    """Kalshi settles into cash with no fill event; the ledger must not keep phantom risk."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.65))
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    run_cycles_with_dip(eng, ex, alc, [58, 57, 46])
    assert len(store.open_trades()) == 1
    # pretend we are live: the exchange reports no position, and the market settled
    s.trading_mode = "live"
    eng.client = KalshiClient("https://fake.kalshi.test/trade-api/v2", api_key_id="k", private_key_pem=rsa_pem,
                              transport=httpx.MockTransport(ex.handler))
    eng.client._backoff = lambda a: None
    ex.settle(alc, "yes")
    assert eng.reconcile_positions() == 1
    t = store.closed_trades()[0]
    assert t["status"] == "settled" and t["exit_price"] == 100 and "reconciled" in t["exit_reason"]


def test_reconcile_is_a_noop_in_paper_mode(tmp_path):
    ex = two_market_exchange()
    eng, store, _ = build(tmp_path, ex, assessment(p_a=0.65))
    run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 46])
    assert eng.reconcile_positions() == 0 and len(store.open_trades()) == 1


LIVE_SHAPE = {
    "status": "active", "yes_ask_dollars": "0.3300", "yes_bid_dollars": "0.3200",
    "no_ask_dollars": "0.6900", "no_bid_dollars": "0.6700", "last_price_dollars": "0.3200",
    "volume_fp": "1000.0", "volume_24h_fp": "900.0", "open_interest_fp": "500.0", "yes_ask_size_fp": "800.0",
}


def live_shape_exchange(starts_at: str | None):
    """Markets exactly as Kalshi returns them today: dollar strings, fixed-point sizes."""
    ex = FakeExchange()
    for tick, sub, ask in (("KXATPMATCH-SAKNAM-SAK", "Rei Sakamoto", "0.3300"),
                           ("KXATPMATCH-SAKNAM-NAM", "Ji Sung Nam", "0.6900")):
        m = {**tennis_market(tick, "KXATPMATCH-SAKNAM", "Sakamoto vs Nam", sub), **LIVE_SHAPE}
        for legacy in ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "volume", "volume_24h", "open_interest"):
            m.pop(legacy, None)
        m["yes_ask_dollars"] = ask
        m["yes_bid_dollars"] = f"{float(ask) - 0.01:.4f}"
        m["no_ask_dollars"] = f"{1 - float(ask) + 0.01:.4f}"
        m["no_bid_dollars"] = f"{1 - float(ask):.4f}"
        if starts_at:
            m["occurrence_datetime"] = starts_at
        m["ticker"] = tick
        ex.add(m)
    return ex


def test_engine_reads_dollar_string_markets(tmp_path):
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ex = live_shape_exchange(started)
    eng, store, _ = build(tmp_path, ex, None)
    eng.cycle()
    row = store.get_state("tennis_watch")[0]
    sak = next(leg for leg in row["legs"] if leg["player"] == "Rei Sakamoto")
    assert (sak["bid"], sak["ask"]) == (32, 33), row["legs"]
    assert row["status"] != "closed"


def test_quote_drift_before_the_start_is_not_live(tmp_path):
    """Quotes drift for days before play. Without trades that is not a live match."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ex = live_shape_exchange(tomorrow)
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.70))
    eng.analyst.result.player_a = eng.analyst.result.a.name = "Rei Sakamoto"
    eng.analyst.result.player_b = eng.analyst.result.b.name = "Ji Sung Nam"
    sak = "KXATPMATCH-SAKNAM-SAK"
    eng.cycle()
    for ask in ("0.3000", "0.2100"):  # a big dip, but nothing actually trades
        ex.markets[sak]["yes_ask_dollars"] = ask
        ex.markets[sak]["yes_bid_dollars"] = f"{float(ask) - 0.01:.4f}"
        res = eng.cycle()
    assert res.trades_opened == 0
    assert any("not live" in sig.rationale for sig in res.signals)


def test_real_trading_outranks_the_schedule(tmp_path):
    """A match that is genuinely trading is live even if the schedule says otherwise.

    Tennis runs late constantly, and `occurrence_datetime` is the *planned* start,
    so the tape has to win. This is the Challenger case: matches in progress whose
    scheduled slot has not arrived, or has long passed.
    """
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ex = live_shape_exchange(tomorrow)
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.70))
    eng.analyst.result.player_a = eng.analyst.result.a.name = "Rei Sakamoto"
    eng.analyst.result.player_b = eng.analyst.result.b.name = "Ji Sung Nam"
    sak = "KXATPMATCH-SAKNAM-SAK"
    eng.cycle()
    for ask in ("0.3000", "0.2100"):  # the same dip, but contracts actually change hands
        ex.markets[sak]["yes_ask_dollars"] = ask
        ex.markets[sak]["yes_bid_dollars"] = f"{float(ask) - 0.01:.4f}"
        ex.markets[sak]["volume_fp"] = str(float(ex.markets[sak]["volume_fp"]) + 150)
        res = eng.cycle()
    assert res.trades_opened == 1, [sig.rationale for sig in res.signals]
    assert store.get_state("tennis_watch")[0]["status"] == "live"


def test_depth_falls_back_to_market_ask_size(tmp_path):
    """An empty order book must not block a market that reports resting size."""
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ex = live_shape_exchange(started)
    ex.depth = 0  # order book comes back empty
    eng, store, s = build(tmp_path, ex, assessment(p_a=0.70))
    eng.analyst.result.player_a = eng.analyst.result.a.name = "Rei Sakamoto"
    eng.analyst.result.player_b = eng.analyst.result.b.name = "Ji Sung Nam"
    sak = "KXATPMATCH-SAKNAM-SAK"
    eng.cycle()
    for ask in ("0.3000", "0.2100"):
        ex.markets[sak]["yes_ask_dollars"] = ask
        ex.markets[sak]["yes_bid_dollars"] = f"{float(ask) - 0.01:.4f}"
        ex.markets[sak]["volume_fp"] = str(float(ex.markets[sak]["volume_fp"]) + 100)
        res = eng.cycle()
    assert res.trades_opened == 1, [sig.rationale for sig in res.signals]


def test_watch_row_reports_contracts_traded(tmp_path):
    """The dashboard shows why a match counts as live, not just that it does."""
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ex = live_shape_exchange(started)
    eng, store, _ = build(tmp_path, ex, None)
    sak = "KXATPMATCH-SAKNAM-SAK"
    eng.cycle()
    assert store.get_state("tennis_watch")[0]["traded"] == 0
    ex.markets[sak]["volume_fp"] = str(float(ex.markets[sak]["volume_fp"]) + 24342)
    eng.cycle()
    row = store.get_state("tennis_watch")[0]
    assert row["traded"] == 24342 and row["status"] == "live"


def test_anthropic_key_from_env_reaches_the_sdk(tmp_path, monkeypatch):
    """A key in .env must be passed to the SDK explicitly.

    Loading a .env file does not populate os.environ, which is the only place the
    Anthropic SDK looks - so without this the client is built with no credentials
    and every research call fails with an authentication error.
    """
    from kalshitrader.config import load_settings
    from kalshitrader.tennis.analyst import TennisAnalyst

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"DB_PATH={tmp_path / 'k.db'}\nANTHROPIC_API_KEY=sk-ant-from-dotenv\n")
    s = load_settings(env)
    assert s.anthropic_api_key == "sk-ant-from-dotenv"
    assert "anthropic_api_key" not in s.as_public_dict()  # never exposed by the API

    captured = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(__import__("sys").modules, "anthropic", type("m", (), {"Anthropic": FakeAnthropic}))
    analyst = TennisAnalyst(Store(str(tmp_path / "k.db")), api_key=s.anthropic_api_key)
    assert analyst.client is not None
    assert captured == {"api_key": "sk-ant-from-dotenv"}


def test_configuration_errors_disable_research_instead_of_retrying(tmp_path):
    """A rejected key fails identically on every match and every scan. Stop trying."""
    from kalshitrader.tennis.analyst import TennisAnalyst

    store = Store(":memory:")

    class Rejecting:
        def __init__(self):
            self.calls = 0

        class messages:  # noqa: N801
            pass

        def create(self, **kw):
            raise RuntimeError("Could not resolve authentication method. Expected one of api_key, ...")

    client = Rejecting()
    client.messages = type("M", (), {"create": lambda _self, **kw: (_ for _ in ()).throw(
        RuntimeError("Could not resolve authentication method. Expected one of api_key, ..."))})()
    analyst = TennisAnalyst(store, client=client)
    ms = [Market.from_api(m) for m in two_market_exchange().markets.values()]
    match = find_contests(ms, ["KXATPMATCH"])[0]

    assert analyst.assess(match) is None
    assert not analyst.available and "key rejected or missing" in analyst.status
    # a second match is not attempted at all while the configuration is broken
    assert analyst.assess(match) is None
    analyst.reset_errors()
    assert analyst.status != "" and analyst._fatal is None


def test_transient_research_errors_do_not_disable_research(tmp_path):
    from kalshitrader.tennis.analyst import TennisAnalyst

    store = Store(":memory:")
    client = type("C", (), {})()
    client.messages = type("M", (), {"create": lambda _self, **kw: (_ for _ in ()).throw(
        RuntimeError("upstream connect error / timeout"))})()
    analyst = TennisAnalyst(store, client=client)
    ms = [Market.from_api(m) for m in two_market_exchange().markets.values()]
    match = find_contests(ms, ["KXATPMATCH"])[0]
    assert analyst.assess(match) is None
    assert analyst.available and analyst.status == "ready"


def test_research_never_blocks_the_trading_loop(tmp_path):
    """Research takes minutes per match. A blocked cycle is a cycle where stop-losses
    are not checked, so it must run off the loop."""
    import threading
    import time

    from kalshitrader.tennis.analyst import TennisAnalyst

    store = Store(":memory:")
    started, release = threading.Event(), threading.Event()

    def slow_create(**kw):
        started.set()
        release.wait(5)
        raise RuntimeError("upstream timeout")  # transient: does not disable research

    client = type("C", (), {})()
    client.messages = type("M", (), {"create": lambda _self, **kw: slow_create(**kw)})()
    analyst = TennisAnalyst(store, client=client)
    ms = [Market.from_api(m) for m in two_market_exchange().markets.values()]
    match = find_contests(ms, ["KXATPMATCH"])[0]

    t0 = time.monotonic()
    assert analyst.request(match) is None       # queues, returns at once
    assert time.monotonic() - t0 < 0.5, "request() blocked the caller"
    assert started.wait(2), "background worker did not pick the match up"
    assert analyst.pending == 1
    # a second request for the same match does not queue it twice
    assert analyst.request(match) is None and analyst.pending == 1
    release.set()
    for _ in range(50):
        if analyst.pending == 0:
            break
        time.sleep(0.05)
    assert analyst.pending == 0
    assert analyst.available  # a timeout is transient, research stays on


def test_research_queue_is_capped(tmp_path):
    """31 live matches must not queue 31 minutes of work ahead of the useful ones."""
    from kalshitrader.tennis.analyst import TennisAnalyst

    store = Store(":memory:")
    client = type("C", (), {})()
    client.messages = type("M", (), {"create": lambda _self, **kw: __import__("time").sleep(30)})()
    analyst = TennisAnalyst(store, client=client)
    analyst.max_queue = 3
    ms = [Market.from_api(m) for m in two_market_exchange().markets.values()]
    base = find_contests(ms, ["KXATPMATCH"])[0]
    for i in range(10):
        clone = Contest(key=f"m{i}", a=base.player_a, b=base.player_b,
                            legs=base.legs, markets=base.markets)
        analyst.request(clone)
    assert analyst.pending <= 3


def test_blocker_tally_groups_reasons_and_collapses_numbers(tmp_path):
    """"Why did nothing trade?" has to be answerable without reading 76 rows, so
    identical reasons about different markets collapse into one counted row."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    s.bot_enabled = False  # one reason, every leg
    eng.cycle()
    rows = store.get_state("tennis_blockers", [])
    assert rows, "no blocker tally was written"
    assert rows[0]["reason"] == "bot disabled"
    assert rows[0]["count"] >= 2
    assert sum(r["count"] for r in rows) >= 2


def test_blocker_tally_strips_the_numbers_that_differ_per_market(tmp_path):
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    s.dip_cents = 90  # nothing dips this far: same sentence, different numbers per leg
    run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 56])
    rows = store.get_state("tennis_blockers", [])
    dips = [r for r in rows if r["reason"].startswith("dip #c < #c")]
    assert len(dips) == 1, [r["reason"] for r in rows]
    assert dips[0]["count"] >= 2
    assert "#" not in dips[0]["example"]  # the example keeps the real numbers


def test_reward_risk_floor_is_measured_after_fees(tmp_path):
    """Kalshi charges on entry and on exit, so the fee comes out of the win and is
    added to the loss. A floor compared against gross cents flatters every trade -
    9c against 8c reads 1.12:1 gross and is 0.49:1 net - so the floor, when set,
    must hold against the net numbers or pass."""
    from kalshitrader.analysis.ev import kalshi_fee_cents

    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    s.stop_loss_cents = 20
    s.min_reward_risk = 1.0
    res = run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 40])
    for sig in [s2 for r in res for s2 in r.signals if s2.is_trade]:
        entry = sig.entry_price
        net_reward = (sig.take_profit - entry) - kalshi_fee_cents(entry, s.fee_rate) - kalshi_fee_cents(sig.take_profit, s.fee_rate)
        net_risk = (entry - sig.stop_loss) + kalshi_fee_cents(entry, s.fee_rate) + kalshi_fee_cents(sig.stop_loss, s.fee_rate)
        assert net_reward >= net_risk, f"{sig.ticker}: {net_risk:.1f}c net risk for {net_reward:.1f}c net reward"


def test_no_profile_claims_a_reward_risk_floor_it_cannot_meet():
    """A floor above 0 makes these profiles pass on everything: at a 14c dip the
    target is 3.5c net against a 9.5c net stop. Shipping one would silently stop the
    bot trading, so every profile leaves the floor off and shows the real dollars."""
    from kalshitrader.analysis.ev import kalshi_fee_cents
    from kalshitrader.config import PROFILES

    for name, profile in PROFILES.items():
        cfg = profile["settings"]
        if cfg["min_reward_risk"] <= 0:
            continue
        ask = 44  # a mid-priced contract, where fees are near their worst
        target = ask + min(cfg["take_profit_cents"], cfg["dip_cents"] // 2)
        stop = ask - cfg["stop_loss_cents"]
        net_reward = (target - ask) - kalshi_fee_cents(ask) - kalshi_fee_cents(target)
        net_risk = (ask - stop) + kalshi_fee_cents(ask) + kalshi_fee_cents(stop)
        assert net_reward >= cfg["min_reward_risk"] * net_risk, (
            f"profile {name} sets a {cfg['min_reward_risk']}:1 floor it cannot reach "
            f"({net_reward:.1f}c net reward vs {net_risk:.1f}c net risk)")


def test_manual_cash_out_sells_regardless_of_target_or_stop(tmp_path):
    """The dashboard has no broker, so a cash-out is a request the trading loop
    fills. Once asked for, the position sells at the bid even though neither the
    target nor the stop has been reached."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    run_cycles_with_dip(eng, ex, alc, [58, 57, 40])
    open_now = store.open_trades()
    assert open_now, "no position to cash out"
    trade = open_now[0]
    # Park the price between the stop and the target: nothing would exit on its own.
    mid = (trade["entry_price"] + trade["take_profit"]) // 2
    ex.set_price(alc, mid - 1, mid)
    assert eng.cycle().trades_closed == 0

    assert store.request_close(trade["id"]) is True
    assert eng.cycle().trades_closed == 1
    closed = store.closed_trades(limit=1)[0]
    assert closed["exit_reason"] == "manual cash-out"
    assert not store.open_trades()


def test_cash_out_of_a_closed_position_is_refused(tmp_path):
    """Two clicks, or a click landing just after the stop fired, must not queue a
    sell against a position that is already gone."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    run_cycles_with_dip(eng, ex, "KXATPMATCH-25SEP07ALCSIN-ALC", [58, 57, 40])
    trade = store.open_trades()[0]
    assert store.request_close(trade["id"]) is True
    eng.cycle()
    assert store.request_close(trade["id"]) is False
    assert store.request_close(999999) is False


def test_one_decline_is_bought_once_not_laddered_into(tmp_path):
    """A live run bought the same player four times as she slid 67c -> 46c, stopping
    out on every one: -$16 on a single match. The dip is measured against a rolling
    high that does not decay, so each new low reads as a fresh dip. One trade per
    market per match."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    # A sustained slide, exactly the shape that produced the four Sabalenka entries.
    run_cycles_with_dip(eng, ex, alc, [70, 67, 60, 54, 48, 42, 36, 30])
    entries = [t for t in store.all_trades() if t["ticker"] == alc]
    assert len(entries) == 1, f"bought the same decline {len(entries)} times: {[t['entry_price'] for t in entries]}"


def test_cooldown_lapses_so_a_later_match_can_be_traded(tmp_path):
    """The cooldown must be a wait, not a permanent ban: the same ticker is fair game
    once the window has passed."""
    from datetime import datetime, timedelta, timezone

    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    s.reentry_cooldown_minutes = 60
    now = datetime.now(timezone.utc)
    assert eng.strategy._cooldown_remaining("KXATPMATCH-NEVER-TRADED", now) is None
    store.open_trade(mode="paper", ticker="KX-T", title="t", side="yes", entry_price=40, count=5,
                     take_profit=48, stop_loss=34, p_true=0.5, fees=0.1, signal_id=None, close_time=None)
    tid = store.open_trades()[0]["id"]
    store.close_trade(tid, exit_price=34, exit_reason="stop-loss")
    assert eng.strategy._cooldown_remaining("KX-T", now) is not None       # just closed
    assert eng.strategy._cooldown_remaining("KX-T", now + timedelta(minutes=61)) is None


def test_riding_a_position_never_stops_out(tmp_path):
    """"Ride it out": with the stop off a position is held to its target or to
    settlement, however far the price falls in between."""
    ex = two_market_exchange()
    eng, store, s = build(tmp_path, ex, None, allow_without_research=True)
    s.use_stop_loss = False
    alc = "KXATPMATCH-25SEP07ALCSIN-ALC"
    run_cycles_with_dip(eng, ex, alc, [58, 57, 40])
    held = [t for t in store.open_trades() if t["ticker"] == alc]
    assert held, "no position was opened"
    assert held[0]["stop_loss"] == 0
    for price in (30, 20, 10, 5):  # a collapse that would have stopped out many times
        ex.set_price(alc, price, price + 2)
        eng.cycle()
    assert [t for t in store.open_trades() if t["ticker"] == alc], "the position was closed despite the stop being off"
