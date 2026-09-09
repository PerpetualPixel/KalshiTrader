from kalshitrader.execution.live import LiveBroker
from kalshitrader.tracking.db import Store


def test_live_broker_places_and_reads_fill(client, exchange, monkeypatch):
    monkeypatch.setattr("kalshitrader.execution.live.time.sleep", lambda s: None)
    b = LiveBroker(client, Store(":memory:"), fill_wait_seconds=0)
    fill = b.buy("KXTEST-1", "yes", 50, 4)
    assert fill.status == "filled" and fill.count == 4 and fill.broker_order_id == "ord-1"
    assert fill.fees_dollars > 0
    assert exchange.orders[0]["yes_price"] == 50 and exchange.orders[0]["action"] == "buy"
    assert b.cash_dollars() == 1000.0


def test_live_broker_records_resting_and_cancels(client, exchange, monkeypatch):
    monkeypatch.setattr("kalshitrader.execution.live.time.sleep", lambda s: None)
    store = Store(":memory:")
    b = LiveBroker(client, store, fill_wait_seconds=0)
    original = exchange.handler

    def resting_handler(request):
        resp = original(request)
        if request.method == "POST" and request.url.path.endswith("/portfolio/orders"):
            exchange.orders[-1].update(status="resting", fill_count=0, remaining_count=4)
            return __import__("httpx").Response(201, json={"order": exchange.orders[-1]})
        return resp

    client._http._transport.handler = resting_handler
    fill = b.sell("KXTEST-1", "yes", 60, 4)
    assert fill.status == "resting" and fill.count == 0
    assert store.pending_orders()[0]["broker_order_id"] == "ord-1"
    assert b.cancel_stale(0) == 1
    assert exchange.orders[0]["status"] == "canceled"
    assert store.pending_orders() == []
