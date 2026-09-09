import pytest
from fastapi.testclient import TestClient

from kalshitrader.config import Settings, load_settings
from kalshitrader.dashboard.app import create_app
from kalshitrader.tracking.db import Store


def make(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"DB_PATH={tmp_path / 'db.sqlite'}\nMANUAL_ESTIMATES_PATH={tmp_path / 'e.json'}\n")
    settings = load_settings(env)
    store = Store(settings.db_path)
    return TestClient(create_app(settings, store, env_file=env)), store, env


def test_overview_and_pages(tmp_path):
    tc, store, _ = make(tmp_path)
    store.set_state("tennis_watch", [{"title": "A vs B", "live": True, "legs": []}])
    assert "KalshiTrader" in tc.get("/").text and "<svg" not in tc.get("/").text[:200]
    assert "Equity curve" in tc.get("/full").text
    o = tc.get("/api/tennis/overview").json()
    assert o["mode"] == "paper" and o["bot_enabled"] is False and o["watch"][0]["title"] == "A vs B"
    assert o["positions"] == []


def test_settings_schema_and_overrides(tmp_path):
    tc, store, _ = make(tmp_path)
    schema = tc.get("/api/settings/schema").json()
    keys = {row["key"] for row in schema}
    assert {"dip_cents", "bot_enabled", "take_profit_cents"} <= keys
    from kalshitrader.config import Settings

    assert next(r for r in schema if r["key"] == "dip_cents")["value"] == Settings().dip_cents
    r = tc.put("/api/settings", json={"dip_cents": 12, "bot_enabled": False})
    assert r.status_code == 200
    assert store.get_state("settings_overrides") == {"dip_cents": 12, "bot_enabled": False}
    assert next(r for r in tc.get("/api/settings/schema").json() if r["key"] == "dip_cents")["value"] == 12
    assert tc.get("/api/tennis/overview").json()["bot_enabled"] is False
    tc.put("/api/settings", json={"bot_enabled": True})
    assert tc.get("/api/tennis/overview").json()["bot_enabled"] is True
    assert tc.put("/api/settings", json={"nope": 1}).status_code == 400
    assert tc.delete("/api/settings").status_code == 200 and store.get_state("settings_overrides") == {}


def test_credentials_roundtrip(tmp_path, rsa_pem):
    tc, _, env = make(tmp_path)
    assert tc.get("/api/credentials").json()["kalshi_api_key_id"] is None
    r = tc.post("/api/credentials", json={"kalshi_api_key_id": "abcd-1234", "kalshi_private_key_pem": rsa_pem, "anthropic_api_key": "sk-ant-xyz9"})
    assert r.status_code == 200, r.text
    c = tc.get("/api/credentials").json()
    assert c["kalshi_api_key_id"] == "…1234" and c["kalshi_private_key"] == "set" and c["anthropic_api_key"] == "…xyz9"
    text = env.read_text()
    assert "KALSHI_API_KEY_ID=abcd-1234" in text and "KALSHI_PRIVATE_KEY_PEM=" in text and "DB_PATH=" in text
    assert tc.post("/api/credentials", json={"kalshi_private_key_pem": "garbage"}).status_code == 400
    assert tc.post("/api/credentials", json={"kalshi_private_key_pem": "-----BEGIN PRIVATE KEY-----\nnotreal\n-----END PRIVATE KEY-----"}).status_code == 400
    assert tc.post("/api/credentials", json={}).status_code == 400
    assert tc.post("/api/credentials", json={"trading_mode": "live", "kalshi_env": "demo"}).status_code == 200
    assert "TRADING_MODE=live" in env.read_text()


def test_credentials_test_endpoint(tmp_path, rsa_pem, monkeypatch):
    from kalshitrader.kalshi import client as client_mod
    from kalshitrader.kalshi.client import KalshiError

    tc, _, env = make(tmp_path)
    # no key yet: market data still probed, key reported missing, research unavailable without a key
    monkeypatch.setattr(client_mod.KalshiClient, "get_markets", lambda self, **kw: [])
    r = tc.post("/api/credentials/test").json()
    assert r["kalshi"] == {"ok": False, "error": "no API key saved yet"} and r["market_data"]["ok"] is True
    assert r["anthropic"]["ok"] is False and r["env"] == "prod"

    tc.post("/api/credentials", json={"kalshi_api_key_id": "abcd-1234"})
    assert "no private key" in tc.post("/api/credentials/test").json()["kalshi"]["error"]
    tc.post("/api/credentials", json={"kalshi_private_key_pem": rsa_pem})
    monkeypatch.setattr(client_mod.KalshiClient, "get_balance", lambda self: {"balance": 12345, "portfolio_value": 20000})
    r = tc.post("/api/credentials/test").json()
    assert r["kalshi"] == {"ok": True, "balance": 123.45, "portfolio_value": 200.0}

    def rejected(self):
        raise KalshiError(401, "invalid signature")

    monkeypatch.setattr(client_mod.KalshiClient, "get_balance", rejected)
    r = tc.post("/api/credentials/test").json()
    assert r["kalshi"]["ok"] is False and "key rejected" in r["kalshi"]["error"]


def test_overview_reports_discovery_and_key_status(tmp_path, rsa_pem):
    tc, store, env = make(tmp_path)
    store.set_state("tennis_discovery", {"exchange": "x", "KXATPMATCH": "error 404: not found"})
    o = tc.get("/api/tennis/overview").json()
    assert o["kalshi_key_set"] is False and o["discovery"]["KXATPMATCH"].startswith("error 404")
    assert "KXATPMATCH" in o["series"]
    tc.post("/api/credentials", json={"kalshi_api_key_id": "k", "kalshi_private_key_pem": rsa_pem})
    assert tc.get("/api/tennis/overview").json()["kalshi_key_set"] is True


def test_dashboard_survives_live_mode_without_keys(tmp_path):
    """Setting Execution=live with no usable key must not stop the dashboard starting."""
    env = tmp_path / ".env"
    env.write_text(f"DB_PATH={tmp_path / 'db.sqlite'}\nTRADING_MODE=live\nKALSHI_ENV=prod\n")
    from kalshitrader.dashboard.app import create_app
    from kalshitrader.tracking.db import Store as S

    tc = TestClient(create_app(store=S(str(tmp_path / "db.sqlite")), env_file=env))
    o = tc.get("/api/tennis/overview").json()
    assert o["mode"] == "paper"  # degraded to safety, not crashed
    assert "live trading requires" in o["config_error"]
    assert tc.get("/").status_code == 200


def test_overview_flags_live_mode(tmp_path, rsa_pem):
    env = tmp_path / ".env"
    pem = rsa_pem.replace("\n", "\\n")
    env.write_text(f'DB_PATH={tmp_path / "db.sqlite"}\nTRADING_MODE=live\nKALSHI_API_KEY_ID=k\nKALSHI_PRIVATE_KEY_PEM="{pem}"\n')
    from kalshitrader.dashboard.app import create_app
    from kalshitrader.tracking.db import Store as S

    tc = TestClient(create_app(store=S(str(tmp_path / "db.sqlite")), env_file=env))
    o = tc.get("/api/tennis/overview").json()
    assert o["mode"] == "live" and o["config_error"] is None


def test_profiles_are_listed_and_applied(tmp_path):
    """One click sets every entry/exit rule and never touches sizing or the pause switch."""
    from kalshitrader.config import PROFILES

    tc, store, env = make(tmp_path)
    listed = tc.get("/api/profiles").json()
    assert {p["name"] for p in listed} == set(PROFILES)
    assert all(p["active"] is False for p in listed)
    assert all(p["label"] and p["blurb"] for p in listed)

    before = tc.get("/api/settings/schema").json()
    sizing_before = {r["key"]: r["value"] for r in before if r["key"].endswith("_dollars")}

    r = tc.post("/api/profiles/risky")
    assert r.status_code == 200 and r.json()["profile"] == "risky"
    after = {r["key"]: r["value"] for r in tc.get("/api/settings/schema").json()}
    assert after["dip_cents"] == PROFILES["risky"]["settings"]["dip_cents"]
    assert after["min_edge_cents"] == PROFILES["risky"]["settings"]["min_edge_cents"]
    # sizing and the pause switch are untouched by a profile
    assert {k: after[k] for k in sizing_before} == sizing_before
    assert "bot_enabled" not in PROFILES["risky"]["settings"]

    assert tc.get("/api/tennis/overview").json()["active_profile"] == "risky"
    assert [p for p in tc.get("/api/profiles").json() if p["active"]][0]["name"] == "risky"

    # switching profile replaces the previous profile's values
    tc.post("/api/profiles/safe")
    safe = {r["key"]: r["value"] for r in tc.get("/api/settings/schema").json()}
    assert safe["dip_cents"] == PROFILES["safe"]["settings"]["dip_cents"]

    # editing a setting by hand means it is no longer a clean profile
    tc.put("/api/settings", json={"dip_cents": 11})
    assert tc.get("/api/tennis/overview").json()["active_profile"] is None
    assert tc.post("/api/profiles/nonsense").status_code == 404


def test_reset_clears_the_active_profile(tmp_path):
    tc, store, _ = make(tmp_path)
    tc.post("/api/profiles/normal")
    tc.delete("/api/settings")
    assert tc.get("/api/tennis/overview").json()["active_profile"] is None
    assert store.get_state("settings_overrides") == {}


def test_overview_reports_dollars_staked_risked_and_won(tmp_path):
    """Cents times contracts is not a number anyone reads at a glance, so the API
    states the stake, the loss if the stop fires and the gain if the target hits -
    all in dollars, net of the exit fee."""
    store = Store(str(tmp_path / "t.db"))
    store.open_trade(mode="paper", ticker="KXATPMATCH-T", title="A vs B · A", side="yes",
                     entry_price=40, count=25, take_profit=50, stop_loss=34, p_true=0.5, fees=0.7, signal_id=None, close_time=None)
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "t.db")), store, env_file=str(tmp_path / ".env")))
    row = client.get("/api/tennis/overview").json()
    pos = row["positions"][0]
    assert pos["cost"] == pytest.approx(10.0)          # 25 x 40c
    assert pos["target_payout"] == pytest.approx(12.5)  # 25 x 50c
    assert 1.0 < pos["target_gain"] < 1.8               # $2.50 gross, less entry and exit fees
    assert 2.0 < pos["at_risk"] < 2.6                   # $1.50 of price, plus both fees
    assert row["open_cost"] == pytest.approx(10.0)


def test_cash_out_endpoint_queues_the_sell_and_refuses_a_closed_trade(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    tid = store.open_trade(mode="paper", ticker="KXATPMATCH-T", title="A vs B · A", side="yes",
                           entry_price=40, count=10, take_profit=50, stop_loss=34, p_true=0.5, fees=0.3, signal_id=None, close_time=None)
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "t.db")), store, env_file=str(tmp_path / ".env")))
    assert client.post(f"/api/positions/{tid}/close").json()["ok"] is True
    assert store.open_trades()[0]["close_requested"] == 1
    store.close_trade(tid, exit_price=45, exit_reason="manual cash-out")
    assert client.post(f"/api/positions/{tid}/close").status_code == 404


def test_metrics_survive_a_run_with_no_losing_trades(tmp_path):
    """Profit factor is undefined until something loses, and comes back as JSON null.
    The dashboard used to call .toFixed() on it - isFinite(null) is true in JavaScript -
    which threw and blanked the whole Tracking tab. The API contract is pinned here so
    the null case stays visible to whoever renders it."""
    store = Store(str(tmp_path / "t.db"))
    tid = store.open_trade(mode="paper", ticker="KX-T", title="A vs B · A", side="yes", entry_price=40,
                           count=10, take_profit=48, stop_loss=0, p_true=0.5, fees=0.3,
                           signal_id=None, close_time=None)
    store.close_trade(tid, exit_price=48, exit_reason="take-profit")
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "t.db")), store, env_file=str(tmp_path / ".env")))
    m = client.get("/api/summary").json()["metrics"]
    assert m["profit_factor"] is None, "no losses means an undefined profit factor, serialised as null"
    assert m["win_rate"] == 1.0
    assert m["expectancy"] > 0
