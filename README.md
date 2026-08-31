# ⚡ KalshiTrader

An automated trading bot for [Kalshi](https://kalshi.com) event contracts with a
local web dashboard: real-time equity curve, PnL metrics, per-strategy
start/pause/stop controls, live settings editor, and a terminal-style activity
stream.

> **Not financial advice. Trading involves risk of loss.** Run in demo mode
> first (see [Going live](#going-live)) and keep allocation limits low.

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│  dashboard/  — vanilla JS + Chart.js, served by FastAPI    │
│      ▲  REST (state, settings, control)                    │
│      ▲  WebSocket /ws (live activity + overview stream)    │
├────────────────────────────────────────────────────────────┤
│  src/main.py        FastAPI app                            │
│  src/bot_engine.py  strategy loops, order gating, equity   │
│  src/strategies/    arbitrage · fair value · signal watch  │
│  src/risk_manager.py  pre-trade checks + circuit breakers  │
│  src/kalshi_client.py RSA-signed async API client          │
│  src/database.py    SQLite via SQLAlchemy (audit trail)    │
└────────────────────────────────────────────────────────────┘
```

The core loop for each running strategy:

1. **Scan** — pull live orderbooks for the configured tickers/series.
2. **Detect edge** — arbitrage spread or fair-value discrepancy.
3. **Risk check** — capital ceiling, per-order size, circuit breakers.
4. **Execute** — limit orders only, each with an `expiration_ts` (TTL) so
   nothing rests stale.

## Strategies

| Strategy | What it does | Places orders? |
| --- | --- | --- |
| **Binary Arbitrage Scanner** | Buys YES + NO together when `yes_ask + no_ask ≤ 100¢ − min_profit`, locking in the spread at settlement | Yes |
| **Fair Value / Edge Trader** | You supply per-ticker fair probabilities (dashboard → Fair Values); it bids when the market is cheaper than fair value minus an edge buffer | Yes |
| **Signal-Only Watcher** | Same detection as the arb scanner, but only emits notifications — for validating thresholds before committing capital | No |

## Risk management

- `max_money_working` — hard ceiling on capital in open orders + positions;
  any order that would exceed it is blocked.
- `max_contracts_per_order` — per-order size cap.
- `daily_stop_loss_pct` — if equity drops more than this % from the day's
  first snapshot, the engine **cancels all resting orders, pauses every
  strategy, and refuses new orders** until you reset the breaker in the UI.
- Limit orders only, every one with an automatic expiration (`order_ttl_seconds`).
- Every order, fill, settings change, and breaker event is written to the
  SQLite audit trail.

## Setup

### 1. Credentials

1. Create an API key at kalshi.com → Account → API keys and download the
   private key `.pem`.
2. Save it as `keys/kalshi_private_key.pem` (the `keys/` dir is gitignored).
3. Copy the env template and fill it in:

```bash
cp .env.example .env
# edit .env: KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV=demo
```

**Never commit `.env` or `.pem` files** — the provided `.gitignore` already
excludes them.

### 2. Install & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn src.main:app --reload
```

Open **http://127.0.0.1:8000** — the dashboard is served by the same process.

### 3. Configure & start

1. In the **Settings** panel set your target tickers (comma-separated market
   tickers) and/or target series (e.g. a crypto series ticker — all its open
   markets are auto-discovered each scan).
2. Tune `Scan Interval`, `Contracts Per Side`, `Min Profit Threshold`, and
   `Max Allocation`, then **Save Settings**.
3. Start the **Signal-Only Watcher** first and watch the activity stream to
   confirm scans run cleanly and edges are detected sensibly.
4. Start the arbitrage or fair-value strategy when you're happy.

### Verification checklist (demo)

- Dashboard shows your demo balance and the equity curve begins plotting.
- Scans appear in the activity log at your configured interval with no
  authentication errors.
- Placed limit orders appear in the Orders table (and in the Kalshi demo UI),
  and expire on their own after the order TTL.

## Going live

- Keep `KALSHI_ENV=demo` for at least ~48h of running to confirm fill
  behavior, fee impact on your thresholds, and that the circuit breakers trip
  the way you expect.
- Kalshi charges trading fees (roughly `0.07 × price × (1−price)` per
  contract, rounded up) — set `Min Profit Threshold` high enough that an arb
  is still profitable after fees on **both** legs.
- Switch to live from the dashboard Mode selector (it requires an explicit
  confirmation) or set `KALSHI_ENV=live` in `.env`. Separate live credentials
  can be provided via `KALSHI_LIVE_KEY_ID` / `KALSHI_LIVE_PRIVATE_KEY_PATH`.
- Start with `Max Allocation` at $25–$50 to verify real execution before
  scaling.

## API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/status` | Engine + per-strategy state |
| GET | `/api/overview` | Equity, cash, PnL, order count |
| GET | `/api/equity_history` | Equity curve snapshots |
| GET | `/api/orders` / `/api/fills` / `/api/activity` | History tables |
| GET | `/api/positions` / `/api/markets` | Live pass-through to Kalshi |
| GET/PUT | `/api/settings` | Read / live-update bot settings |
| POST | `/api/bot/{strategy}/{start\|pause\|stop}` | Bot controls |
| POST | `/api/orders/cancel_all` | Cancel every resting order |
| POST | `/api/risk/reset` | Clear a tripped circuit breaker |
| WS | `/ws` | Live activity + overview stream |

## Tests

```bash
pytest
```

Covers the risk manager (ceilings, stop-loss breaker) and the API client
(RSA-PSS signature correctness, orderbook price derivation, input validation).

## Notes

- All prices are integer cents (1–99); all money values are integer cents.
- API hosts default to `api.elections.kalshi.com` (live) and
  `demo-api.kalshi.co` (demo); override with `KALSHI_API_BASE` if Kalshi
  moves hosts again.
- The dashboard binds to `127.0.0.1` by default and has no authentication —
  don't expose the port publicly.
