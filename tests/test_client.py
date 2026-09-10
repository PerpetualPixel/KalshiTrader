from datetime import datetime, timezone

import httpx
import pytest

from kalshitrader.kalshi.client import KalshiClient, KalshiError
from tests.conftest import make_market


def test_get_markets_and_orderbook(client, exchange):
    exchange.add(make_market("KXTEST-2", yes_bid=30, yes_ask=33))
    markets = client.get_markets()
    assert {m.ticker for m in markets} == {"KXTEST-1", "KXTEST-2"}
    m2 = next(m for m in markets if m.ticker == "KXTEST-2")
    assert m2.yes_spread == 3 and m2.no_ask == 70
    book = client.get_orderbook("KXTEST-2")
    assert book.best_yes_bid == 30
    assert book.best_yes_ask == 33  # 100 - best no bid (67)
    assert book.depth_at_yes_ask() == exchange.depth


def test_market_helpers():
    m = __import__("kalshitrader.kalshi.models", fromlist=["Market"]).Market.from_api(make_market(yes_bid=40, yes_ask=44, hours_to_close=10))
    assert 9.9 < m.hours_to_close() < 10.1
    assert m.yes_mid == 42.0
    assert not m.is_settled()


def test_private_endpoints_are_signed(client, exchange):
    bal = client.get_balance()
    assert bal["balance"] == 100_000
    req = exchange.requests[-1]
    assert req.headers["KALSHI-ACCESS-KEY"] == "key-123"


def test_create_order_shapes_body(client, exchange):
    order = client.create_order(ticker="KXTEST-1", action="buy", side="no", count=3, price_cents=55)
    assert order["order_id"] == "ord-1"
    assert exchange.orders[0]["no_price"] == 55 and "yes_price" not in exchange.orders[0]
    with pytest.raises(ValueError):
        client.create_order(ticker="X", action="buy", side="yes", count=1, price_cents=0)


def test_unauthenticated_client_refuses_private_calls(exchange):
    import httpx

    c = KalshiClient("https://fake.kalshi.test/trade-api/v2", transport=httpx.MockTransport(exchange.handler))
    with pytest.raises(KalshiError) as exc:
        c.get_balance()
    assert exc.value.status == 401
    assert c.get_markets()  # public still works


def test_retries_on_server_error_then_succeeds(client, exchange):
    exchange.fail_next = [503]
    assert client.get_market("KXTEST-1").ticker == "KXTEST-1"


def test_gives_up_after_retries(client, exchange):
    exchange.fail_next = [500, 500, 500, 500]
    with pytest.raises(KalshiError) as exc:
        client.get_market("KXTEST-1")
    assert exc.value.status == 500


def test_404_raises_immediately(client, exchange):
    with pytest.raises(KalshiError) as exc:
        client.get_market("NOPE")
    assert exc.value.status == 404
    assert len([r for r in exchange.requests if "NOPE" in str(r.url)]) == 1


def test_clock_drift_is_learned_from_the_date_header(client, exchange, rsa_pem):
    """A drifted local clock produces 401s that look like a bad key. Correct for it."""
    import email.utils
    import time

    skewed = time.time() + 600  # server is 10 minutes ahead of us
    original = exchange.handler

    def handler(request):
        resp = original(request)
        resp.headers["date"] = email.utils.formatdate(skewed, usegmt=True)
        return resp

    client._http._transport.handler = handler
    assert client.signer.drift_ms == 0
    client.get_balance()
    assert 590_000 < client.signer.drift_ms < 610_000
    # the next signature carries the offset
    ts = int(client.signer.headers("GET", "https://x/trade-api/v2/p")["KALSHI-ACCESS-TIMESTAMP"])
    assert ts > (time.time() + 500) * 1000


def test_no_drift_correction_for_small_differences(client, exchange):
    client.get_balance()
    assert client.signer.drift_ms == 0


def test_retry_after_is_honoured_on_429(client, exchange, monkeypatch):
    slept = []
    monkeypatch.setattr("kalshitrader.kalshi.client.time.sleep", lambda s: slept.append(s))
    client._backoff = KalshiClient._backoff.__get__(client)  # restore real backoff
    original = exchange.handler
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, json={"error": "slow down"})
        return original(request)

    client._http._transport.handler = handler
    assert client.get_market("KXTEST-1").ticker == "KXTEST-1"
    assert slept == [3.0]


def test_401_is_retried_once_after_resync(client, exchange):
    original = exchange.handler
    calls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/portfolio/balance"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(401, json={"error": "invalid signature"})
        return original(request)

    client._http._transport.handler = handler
    assert client.get_balance()["balance"] == 100_000
    assert calls["n"] == 2


# The exact payload Kalshi returns today: prices as decimal dollar strings and
# counts as fixed-point strings. Captured from the live API.
LIVE_PAYLOAD = {
    "ticker": "KXATPMATCH-26SEP09ZVEVAN-ZVE", "title": "Alexander Zverev wins", "status": "active",
    "event_ticker": "KXATPMATCH-26SEP09ZVEVAN", "yes_sub_title": "Alexander Zverev",
    "yes_ask_dollars": "0.8800", "yes_bid_dollars": "0.8700", "no_ask_dollars": "0.1300", "no_bid_dollars": "0.1200",
    "last_price_dollars": "0.8800", "volume_fp": "5836.05", "volume_24h_fp": "5826.05", "open_interest_fp": "5777.39",
    "liquidity_dollars": "0.0000", "occurrence_datetime": "2026-09-09T18:30:00Z",
    "close_time": "2026-09-23T15:30:00Z", "expected_expiration_time": "2026-09-09T18:30:00Z",
}


def test_parses_dollar_string_prices():
    from kalshitrader.kalshi.models import Market

    m = Market.from_api(LIVE_PAYLOAD)
    assert (m.yes_bid, m.yes_ask, m.no_bid, m.no_ask) == (87, 88, 12, 13)
    assert m.last_price == 88 and m.yes_spread == 1 and m.has_quote
    assert (m.volume, m.volume_24h, m.open_interest) == (5836, 5826, 5777)
    assert m.subtitle == "Alexander Zverev" and m.is_tradeable
    # Pinned against a fixed clock: "has this started" is relative to now, and a
    # bare has_started() turns this into a test that passes until the date goes by.
    assert m.starts_at == datetime(2026, 9, 9, 18, 30, tzinfo=timezone.utc)
    assert not m.has_started(datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc))
    assert m.has_started(datetime(2026, 9, 9, 19, 0, tzinfo=timezone.utc))


def test_still_parses_legacy_integer_cent_fields():
    from kalshitrader.kalshi.models import Market

    m = Market.from_api({"ticker": "X", "yes_bid": 40, "yes_ask": 42, "no_bid": 58, "no_ask": 60,
                         "volume": 10, "volume_24h": 7, "open_interest": 3, "last_price": 41})
    assert (m.yes_bid, m.yes_ask, m.no_bid, m.no_ask, m.last_price) == (40, 42, 58, 60, 41)
    assert (m.volume, m.volume_24h, m.open_interest) == (10, 7, 3)
    assert m.starts_at is None and m.has_started()  # unknown schedule: assume started


def test_price_to_cents_edges():
    from kalshitrader.kalshi.models import price_to_cents

    assert price_to_cents("0.8800") == 88
    assert price_to_cents("1.0000") == 100
    assert price_to_cents("0.0100") == 1
    assert price_to_cents(0.32) == 32
    assert price_to_cents(32) == 32
    assert price_to_cents("32") == 32
    assert price_to_cents(None) == 0 and price_to_cents("") == 0 and price_to_cents("abc") == 0


def test_orderbook_accepts_both_shapes():
    from kalshitrader.kalshi.models import Orderbook

    dollars = Orderbook.from_api("X", {"orderbook": {"yes": [["0.32", "500.0"], ["0.31", "20"]], "no": [["0.67", "400"]]}})
    assert dollars.yes == [(32, 500), (31, 20)] and dollars.best_yes_ask == 33
    cents = Orderbook.from_api("X", {"orderbook": {"yes": [[32, 500]], "no": [[67, 400]]}})
    assert cents.yes == [(32, 500)] and cents.best_yes_ask == 33
    assert Orderbook.from_api("X", {"orderbook": {"yes": [], "no": None}}).yes == []
    # junk levels are dropped, not crashed on
    assert Orderbook.from_api("X", {"orderbook": {"yes": [["x", "y"], [0, 5], [30, 0]]}}).yes == []


# The order book exactly as Kalshi returns it today, captured from the live API.
LIVE_ORDERBOOK = {
    "orderbook_fp": {
        "no_dollars": [["0.0800", "11485.26"], ["0.0900", "13414.00"], ["0.1000", "64407.91"],
                       ["0.1100", "49395.12"], ["0.1200", "3414.41"]],
        "yes_dollars": [["0.8300", "0.01"], ["0.8400", "8558.01"], ["0.8500", "24812.01"],
                        ["0.8600", "34436.77"], ["0.8700", "50347.42"]],
    }
}


def test_parses_live_orderbook_fp_shape():
    from kalshitrader.kalshi.models import Orderbook

    book = Orderbook.from_api("KXATPMATCH-26SEP09ZVEVAN-ZVE", LIVE_ORDERBOOK)
    assert book.best_yes_bid == 87 and book.best_no_bid == 12
    # yes ask is derived from the best no bid, and matches the market's own yes_ask
    assert book.best_yes_ask == 88 and book.best_no_ask == 13
    # depth at the yes ask equals the market's reported yes_ask_size (3414.41)
    assert book.depth_at_yes_ask() == 3414
    # the 0.01-contract dust level is dropped rather than becoming a phantom level
    assert (83, 0) not in book.yes and len(book.yes) == 4


def test_orderbook_and_market_agree_on_the_live_payload():
    """The book's derived prices must match the market's own quote fields."""
    from kalshitrader.kalshi.models import Market, Orderbook

    m = Market.from_api(LIVE_PAYLOAD)
    book = Orderbook.from_api(m.ticker, LIVE_ORDERBOOK)
    assert (book.best_yes_bid, book.best_yes_ask) == (m.yes_bid, m.yes_ask)
    assert (book.best_no_bid, book.best_no_ask) == (m.no_bid, m.no_ask)
