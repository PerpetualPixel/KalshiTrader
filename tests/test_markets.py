"""The generic contest model and series discovery - the parts that make this bot
work on any Kalshi market rather than only on tennis."""
from kalshitrader.kalshi.models import Market
from kalshitrader.markets.contest import NO_SIDE, find_contests, parse_sides
from kalshitrader.markets.discovery import by_category, categorise, summarise
from tests.conftest import make_market


def market(ticker, *, event=None, series="KXTEST", title="A market", subtitle="", volume=100,
           yes_bid=40, yes_ask=42, oi=0):
    m = make_market(ticker, yes_bid=yes_bid, yes_ask=yes_ask, volume_24h=volume, hours_to_close=6, title=title)
    m.update(event_ticker=event or ticker, series_ticker=series, yes_sub_title=subtitle, open_interest=oi)
    return Market.from_api(m)


# ------------------------------------------------------------------ shapes
def test_head_to_head_split_across_two_markets():
    """Most sports: one market per competitor, each Yes meaning "this one wins"."""
    ms = [market("KXNFLGAME-KC", event="KXNFLGAME-1", series="KXNFLGAME", subtitle="Chiefs", yes_ask=55),
          market("KXNFLGAME-BUF", event="KXNFLGAME-1", series="KXNFLGAME", subtitle="Bills", yes_ask=47)]
    c = find_contests(ms, ["KXNFLGAME"])
    assert len(c) == 1 and {c[0].a, c[0].b} == {"Chiefs", "Bills"}
    assert c[0].leg("Chiefs").side == "yes" and c[0].leg("Bills").side == "yes"
    assert c[0].leg("Chiefs").ask == 55


def test_head_to_head_in_one_market():
    ms = [market("KXEPL-1", series="KXEPL", title="Arsenal vs Chelsea")]
    c = find_contests(ms, ["KXEPL"])
    assert len(c) == 1 and (c[0].a, c[0].b) == ("Arsenal", "Chelsea")
    assert c[0].leg("Arsenal").side == "yes" and c[0].leg("Chelsea").side == "no"


def test_a_threshold_market_is_a_yes_no_contest():
    """Crypto, weather and economics are not head-to-head: one market, two sides."""
    ms = [market("KXBTC-60K", series="KXBTC", title="Will Bitcoin close above $60,000?", subtitle="Above $60,000")]
    c = find_contests(ms, ["KXBTC"])
    assert len(c) == 1
    assert c[0].a == "Above $60,000" and c[0].b == NO_SIDE
    assert c[0].leg("Above $60,000").side == "yes" and c[0].leg(NO_SIDE).side == "no"
    assert c[0].title == "Above $60,000", "a threshold has no opponent to name"


def test_a_strip_of_thresholds_becomes_one_contest_each():
    """A price strip or a temperature band shares an event ticker but each level is a
    separate bet: pricing them as one contest would pair unrelated outcomes."""
    ms = [market(f"KXHIGHNY-{t}", event="KXHIGHNY-DEC01", series="KXHIGHNY",
                 title=f"High temp {t}F or above", subtitle=f"{t}F or above") for t in (40, 45, 50, 55)]
    c = find_contests(ms, ["KXHIGHNY"])
    assert len(c) == 4, "four thresholds are four bets, not one four-way match"
    assert {x.a for x in c} == {"40F or above", "45F or above", "50F or above", "55F or above"}
    assert all(x.b == NO_SIDE and len(x.legs) == 2 for x in c)
    assert len({x.key for x in c}) == 4, "each needs its own key or they overwrite each other"


def test_only_enabled_series_are_scanned():
    ms = [market("KXNFLGAME-1", series="KXNFLGAME", title="Chiefs vs Bills"),
          market("KXBTC-1", series="KXBTC", title="Will Bitcoin close above $60,000?")]
    assert [c.series for c in find_contests(ms, ["KXBTC"])] == ["KXBTC"]
    assert len(find_contests(ms, ["KXBTC", "KXNFLGAME"])) == 2
    assert find_contests(ms, []) == []


def test_prefix_switches_on_a_whole_family():
    ms = [market("KXNFLGAME-1", series="KXNFLGAME", title="Chiefs vs Bills"),
          market("KXNFLSB-1", series="KXNFLSB", title="Chiefs vs Eagles")]
    assert len(find_contests(ms, [], ["KXNFL"])) == 2


def test_parse_sides_handles_the_shapes_and_rejects_the_rest():
    assert parse_sides("Chiefs vs Bills") == ("Chiefs", "Bills")
    assert parse_sides("Will Arsenal vs Chelsea?") == ("Arsenal", "Chelsea")
    assert parse_sides("Bitcoin above 60k?") is None


# --------------------------------------------------------------- discovery
def test_discovery_ranks_by_volume_and_groups_by_category():
    ms = [market("KXBTC-1", series="KXBTC", volume=9000, oi=50),
          market("KXATPMATCH-1", series="KXATPMATCH", volume=300),
          market("KXATPMATCH-2", series="KXATPMATCH", volume=200),
          market("KXNFLGAME-1", series="KXNFLGAME", volume=5000)]
    found = summarise(ms)
    assert [s.ticker for s in found] == ["KXBTC", "KXNFLGAME", "KXATPMATCH"], "volume first"
    tennis = next(s for s in found if s.ticker == "KXATPMATCH")
    assert tennis.markets == 2 and tennis.volume_24h == 500
    assert {s.category for s in found} == {"Crypto", "Football", "Tennis"}
    assert list(by_category(found))[0] == "Crypto", "the busiest category leads"


def test_a_series_with_no_two_sided_quote_is_marked_illiquid():
    """Counting markets is not counting tradeable markets: a series full of one-sided
    books looks big and cannot be traded."""
    ms = [market("KXDEAD-1", series="KXDEAD", volume=8000, yes_bid=0, yes_ask=0)]
    found = summarise(ms)
    assert found[0].markets == 1 and found[0].liquid_markets == 0


def test_unknown_tickers_are_still_tradeable_just_uncategorised():
    assert categorise("KXSOMETHINGNEW") == "Other"
    assert summarise([market("KXSOMETHINGNEW-1", series="KXSOMETHINGNEW")])[0].category == "Other"


# ------------------------------------------------- packaging readiness
def test_state_paths_move_out_of_the_install_directory_when_frozen(monkeypatch, tmp_path):
    """A packaged build must not write its database next to the executable: someone
    who double-clicks it in Downloads would get their trades and keys in a folder that
    may be read-only, cloud-synced or cleared. A source checkout is unaffected."""
    from kalshitrader import paths

    monkeypatch.setattr(paths, "frozen", lambda: False)
    assert paths.resolve_state_path("data/x.db") == "data/x.db"

    monkeypatch.setattr(paths, "frozen", lambda: True)
    monkeypatch.setenv("KALSHITRADER_HOME", str(tmp_path / "home"))
    assert paths.resolve_state_path("data/x.db") == str(tmp_path / "home" / "x.db")
    assert paths.resolve_state_path("/tmp/explicit.db") == "/tmp/explicit.db", "an absolute path is honoured"


def test_bundled_assets_resolve_in_both_layouts(monkeypatch, tmp_path):
    """Under PyInstaller the package is unpacked to a temp dir named by `sys._MEIPASS`,
    so the dashboard's files are not beside `__file__`."""
    import sys

    from kalshitrader import paths

    assert paths.asset("dashboard", "static").exists(), "found in a source checkout"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.asset("dashboard", "static") == tmp_path / "kalshitrader" / "dashboard" / "static"
