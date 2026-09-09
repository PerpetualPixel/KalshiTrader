from fastapi.testclient import TestClient

from kalshitrader.analysis.estimators import ManualEstimator
from kalshitrader.dashboard.app import create_app
from kalshitrader.execution.paper import PaperBroker
from kalshitrader.strategy.engine import Engine


def test_dashboard_endpoints(settings, client, exchange, store):
    ManualEstimator(settings.manual_estimates_path).set("KXTEST-1", 0.75, 0.9)
    engine = Engine(settings, client, store, PaperBroker(store, 1000.0, 0))
    engine.cycle()
    exchange.set_price("KXTEST-1", 72, 74)
    engine.cycle()

    app = create_app(settings, store)
    tc = TestClient(app)
    assert tc.get("/api/health").json()["ok"]
    assert "<title>KalshiTrader</title>" in tc.get("/").text and "<title>KalshiTrader</title>" in tc.get("/full").text
    s = tc.get("/api/summary").json()
    assert s["mode"] == "paper" and s["metrics"]["trades_closed"] == 1 and s["halted"] is False
    assert s["starting_cash"] == 1000.0
    assert len(tc.get("/api/equity").json()) == 2
    assert tc.get("/api/trades?status=closed").json()[0]["exit_reason"] == "take-profit"
    assert tc.get("/api/trades?status=open").json() == []
    assert tc.get("/api/signals?trades_only=true").json()[0]["action"] == "BUY YES"
    assert tc.get("/api/orders").json()[0]["action"] == "sell"
    assert tc.get("/api/runs").json()[0]["trades_closed"] == 1
    assert tc.get("/api/markets").json()[0]["ticker"] == "KXTEST-1"
    assert len(tc.get("/api/markets/kxtest-1/history").json()) >= 2
    assert "kalshi_api_key_id" not in tc.get("/api/settings").json()

    assert tc.post("/api/control/halt").json()["halted"] is True
    assert tc.get("/api/summary").json()["halted"] is True
    assert tc.post("/api/control/resume").json()["halted"] is False

    r = tc.post("/api/estimates", json={"ticker": "kxfoo", "p_yes": 0.61, "note": "n"})
    assert r.status_code == 200 and tc.get("/api/estimates").json()["KXFOO"]["p_yes"] == 0.61
    assert tc.post("/api/estimates", json={"ticker": "x", "p_yes": 1.5}).status_code == 422
    assert tc.delete("/api/estimates/KXFOO").status_code == 200
    assert tc.delete("/api/estimates/KXFOO").status_code == 404
