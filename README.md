# KalshiTrader

An automated trading bot for [Kalshi](https://kalshi.com) event contracts, with a
local dashboard. It buys price dips on the markets you switch on and sells the
rebound, sizing every position against the fees Kalshi charges on both ends.

> **Not financial advice, and this can lose money.** It ships paused, in paper mode,
> and every number it shows is after fees. Run it on paper until its win rate beats
> the break-even it prints for you. See [Reading the numbers](#reading-the-numbers).

## What it does

Every scan it reads the markets you enabled, groups them into contests, and for each
side asks a series of questions in order — is it live, has it dipped far enough, does
the edge survive both fees, is the book deep enough, is there room under your
exposure limits. It buys when all of them pass, and manages the exit itself.

When it buys nothing, it tells you which question failed and how often. That panel is
the point: "0 entries" on its own sends you hunting for a network fault that was
really a setting.

## Markets

There is **no hard-coded list of tradeable series**, on purpose. A ticker guessed from
memory looks exactly like a market with nothing trading in it, and the difference is
invisible until a whole session passes without a trade.

Instead the bot asks the exchange. Press **Scan the exchange** on the dashboard (or run
`kalshitrader discover`) and it pages the open markets, groups them by series, and ranks
them by 24-hour volume and by how many have a two-sided quote — a series with none of
those cannot be traded however large it looks. Switch on the ones you want; each is a
button.

It handles the shapes Kalshi actually uses:

| Shape | Example | Becomes |
| --- | --- | --- |
| head-to-head, one market per side | most sports | two legs, one per competitor |
| head-to-head, single market | `Arsenal vs Chelsea` | a Yes leg and a No leg |
| single outcome | `Will the Fed cut?` | a Yes leg and a No leg |
| a strip of thresholds | temperature bands, price ladders | one contest per level |

Optional research (currently tennis only, needs an Anthropic key) adds a fair-value
estimate on top. Without it the bot trades the price action alone, which is the default.

## Quick start

```bash
pip install -e ".[dev,ai]"
cp .env.example .env          # then add your Kalshi key id and private key path
kalshitrader-app              # dashboard + bot in one process, opens your browser
```

The bot starts **paused**. Add your keys under Settings, scan for markets, switch some
on, pick a profile, then press Resume.

Prefer separate windows? `kalshitrader dashboard` and `kalshitrader run`, or the
`start.ps1` / `start.sh` launchers.

## Profiles

One click sets the entry and exit rules. None of them touch your keys or the pause
switch, so switching profile cannot change what is already at risk.

| | Dip required | Target | Stop | Break-even win rate |
| --- | --- | --- | --- | --- |
| **Risky** | 10¢ | 6¢ | 5¢ | ~50% |
| **Normal** | 14¢ | 8¢ | 6¢ | ~46% |
| **Safe** | 20¢ | 12¢ | 6¢ | ~38% |

Those break-evens are gross of fees. The honest, after-fee figures are worse and the
dashboard shows them per position — see below.

## Reading the numbers

Kalshi charges `7% × p × (1−p)` per contract **on entry and again on exit**, roughly
3.5¢ round trip at mid prices. That comes out of every win and is added to every loss,
which changes the arithmetic more than it looks:

```
entry 44c, target 53c, stop 36c
gross   reward 9c    risk 8c     1.12:1     ← what the price move suggests
net     reward 5.5c  risk 11.3c  0.49:1     ← what you actually get
```

So each open position shows **Staked**, **If it wins** and **If stopped** in dollars,
after both fees, and the Tracking tab totals them. Judge a run on **win rate against
the break-even those numbers imply**, not on any single trade.

Two switches matter here:

- **Use a stop loss** — off means a position rides to its target or to settlement. You
  risk the whole stake, but pay no exit fee on losers and are never shaken out by noise.
- **Wait before re-buying** — the dip is measured against a rolling high that does not
  decay, so a market sliding all session reads as a fresh dip at every new low. The
  default 180 minutes means one decline gets one trade.

## Backtesting

Every scan records a snapshot of every market it watches, so the settings questions
are answerable from data you already have, in seconds, instead of an hour of real
trading:

```bash
kalshitrader backtest                    # the settings you are running now
kalshitrader backtest --dip 20 --no-stop # try something else
kalshitrader backtest --sweep stop       # compare a range of one setting
```

```
   stop  closed  open   win%  break-even       P&L  per trade     fees
      0       2     4   100%           -     +0.73      +0.37     0.89
      4       9     0    11%         52%     -7.77      -0.86     3.47
      6       9     0    22%         68%     -8.06      -0.90     3.48
      8       7     1    14%         59%     -7.70      -1.10     2.60
```

**Break-even** is the win rate that configuration needed to stand still, computed from
what its own trades actually returned rather than from the configured target and stop.
Beat it and it made money.

It replays through the same `SwingStrategy` the live loop runs — a test fails if the
two ever disagree on the same history. Three honest limits: it works one ticker at a
time (snapshots record prices, not titles), so research and form are not replayed;
fills are taken at the quoted price plus the paper broker's slippage, which is
optimistic on a thin book; and it can only replay markets the bot was watching.

### What crossing the spread costs

```bash
kalshitrader backtest --compare-execution
```

```
            closed  open   win%       P&L  per trade     fees  fill rate
taker            7     1    14%     -7.70      -1.10     2.60          -
maker            7     1    14%     -4.34      -0.62     2.60       100%
```

The bot currently crosses the spread on both sides. Posting at the bid and selling at
the ask saves it — at a 2¢ spread that is worth about as much as the entire net edge.
The catch is that a posted order only fills when somebody trades against it, so the
replay models that rather than assuming it: a buy posted at B fills when a later
snapshot shows the bid at or below B *and* the volume counter has moved.

That model cannot see the queue ahead of you, so **treat the fill rate as a ceiling**.
Execution is still taker-only in live and paper trading; measure first.

Watch the **open** column. A configuration that simply holds its losers shows a
flattering P&L because only closed trades count.

## Commands

| | |
| --- | --- |
| `kalshitrader-app` | dashboard and bot together, one process |
| `kalshitrader discover` | what is trading on Kalshi now, ranked |
| `kalshitrader backtest` | replay recorded prices through the strategy |
| `kalshitrader scan` | one read-only pass; prints signals, places nothing |
| `kalshitrader run` | the trading loop |
| `kalshitrader dashboard` | the dashboard only |
| `kalshitrader diagnose` | what Kalshi returns for your enabled series |
| `kalshitrader report` | win rate, expectancy, profit factor, fees |
| `kalshitrader balance` | your Kalshi balance and positions |
| `kalshitrader halt` | stop new entries; exits keep running |

## Going live

`TRADING_MODE=live` in `.env`, or Settings → Execution. It makes you type `LIVE` and
prints your balance and caps first. Paper mode reads real prices and never sends an
order, so it is a genuine dry run rather than a simulation.

## Packaging

The code is arranged so it can be frozen into a single downloadable executable that
someone runs and pastes their keys into:

- `kalshitrader/app.py` is one entry point that runs dashboard and bot together.
- `kalshitrader/paths.py` resolves bundled assets through `sys._MEIPASS`, so the
  dashboard finds its files inside a bundle, and moves the database and settings to
  the per-user application directory rather than writing beside the executable.
- Imports are static, so a bundler can trace them.

The build script itself is not written yet — that is the remaining step.

## Layout

```
kalshitrader/
  app.py          one-process launcher (dashboard + bot)
  cli.py          every command
  paths.py        assets and writable state, in a checkout or a bundle
  kalshi/         signed API client and market models
  markets/        contest model + series discovery
  backtest/       replay recorded prices through the real strategy
  trading/        the loop and the swing strategy
  analysis/       expected value, fees, signals
  risk/           sizing, exposure caps, circuit breakers
  execution/      paper and live brokers
  tracking/       SQLite store and metrics
  dashboard/      FastAPI + a dependency-free front end
  tennis/         optional per-player research
```
