"""Command-line entry point: `kalshitrader <command>`."""
from __future__ import annotations

import argparse
import json
import sys

from kalshitrader import __version__
from kalshitrader.config import Settings, load_settings
from kalshitrader.log import setup_logging


def _client(s: Settings):
    from kalshitrader.kalshi.client import KalshiClient

    return KalshiClient(
        s.base_url,
        api_key_id=s.kalshi_api_key_id,
        private_key_path=s.kalshi_private_key_path,
        private_key_pem=s.kalshi_private_key_pem,
    )


def _engine(s: Settings, execute_live_ok: bool = True, env_file: str = ".env"):
    from kalshitrader.execution.live import LiveBroker
    from kalshitrader.execution.paper import PaperBroker
    from kalshitrader.strategy.engine import Engine
    from kalshitrader.tracking.db import Store
    from kalshitrader.trading.engine import TennisEngine

    client = _client(s)
    store = Store(s.db_path)
    if s.is_live and execute_live_ok:
        broker = LiveBroker(client, store, fee_rate=s.fee_rate)
    else:
        broker = PaperBroker(store, s.paper_starting_cash, s.paper_slippage_cents, s.fee_rate)
    if s.bot_mode == "tennis":
        return TennisEngine(s, client, store, broker, env_file=env_file)
    return Engine(s, client, store, broker)


# ------------------------------------------------------------------ commands
def cmd_scan(args, s: Settings) -> int:
    engine = _engine(s, execute_live_ok=False, env_file=args.env_file)
    if s.bot_mode == "tennis":
        engine.refresh_settings()
    res = engine.scan(execute=False)
    if s.bot_mode == "tennis":
        print(f"tennis: {len(engine.matches)} matches found in {', '.join(s.enabled_series) or 'all markets'}; research: {engine.analyst.status}")
        for row in store_watch(engine):
            live = "LIVE" if row["live"] else "pre "
            legs = "  ".join(f"{leg['player']} {leg['bid']}/{leg['ask']}c" + (f" form {leg['form']:.0f}" if leg.get("form") is not None else "") for leg in row["legs"])
            print(f"  [{live}] {row['title'][:44]:44s} {legs}")
        print()
    trades = res.trade_signals
    print(f"scanned {res.markets_scanned} markets, {len(res.signals)} evaluated, {len(trades)} actionable\n")
    shown = trades if not args.all else res.signals
    for sig in sorted(shown, key=lambda x: -x.ev_net)[: args.limit]:
        print(sig.format())
        print()
    if not trades:
        print("No actionable signals. Default posture is PASS / HOLD.")
    return 0


def _confirm_live(args, s: Settings) -> None:
    """Spell out what real-money mode actually risks before letting it start."""
    print("", file=sys.stderr)
    print("  *** LIVE MODE - REAL MONEY ***", file=sys.stderr)
    print(f"  exchange        {s.base_url}", file=sys.stderr)
    try:
        balance = _client(s).get_balance().get("balance", 0) / 100
        print(f"  your balance    ${balance:,.2f}", file=sys.stderr)
    except Exception as exc:
        print(f"  your balance    could not be read ({str(exc)[:60]})", file=sys.stderr)
    print(f"  per position    ${s.max_position_dollars:,.2f}", file=sys.stderr)
    print(f"  total exposure  ${s.max_total_exposure_dollars:,.2f} across up to {s.max_open_positions} positions", file=sys.stderr)
    print(f"  daily loss cap  ${s.daily_loss_limit_dollars:,.2f}", file=sys.stderr)
    if not s.anthropic_api_key:
        print("  research        NO ANTHROPIC KEY - every match will PASS, nothing will trade", file=sys.stderr)
    print("  Switch Execution to paper in the dashboard to practise with fake money instead.", file=sys.stderr)
    print("", file=sys.stderr)
    if args.yes:
        return
    if input("Type LIVE to trade real money, anything else to abort: ").strip() != "LIVE":
        print("aborted - nothing was traded")
        raise SystemExit(1)


def store_watch(engine) -> list[dict]:
    return engine.store.get_state("tennis_watch", [])


def cmd_run(args, s: Settings) -> int:
    if s.is_live:
        _confirm_live(args, s)
    engine = _engine(s, env_file=args.env_file)
    if args.once:
        res = engine.cycle()
        print(f"cycle: {res.markets_scanned} markets, {len(res.trade_signals)} trade signals, "
              f"{res.trades_opened} opened, {res.trades_closed} closed, {res.duration_ms}ms"
              + (f", ERROR: {res.error}" if res.error else ""))
        return 1 if res.error else 0
    engine.run_forever(interval=args.interval, max_cycles=args.cycles)
    return 0


def cmd_dashboard(args, s: Settings) -> int:
    from kalshitrader.dashboard.app import serve

    if args.host:
        s.dashboard_host = args.host
    if args.port:
        s.dashboard_port = args.port
    print(f"dashboard: http://{s.dashboard_host}:{s.dashboard_port}")
    serve(s, env_file=args.env_file)
    return 0


def cmd_tennis_analyze(args, s: Settings) -> int:
    """Read a tennis screenshot, research both players, and say who is buyable on a dip."""
    from kalshitrader.tennis.screenshot import analyze_screenshot_bytes
    from kalshitrader.tracking.db import Store

    result = analyze_screenshot_bytes(args.image, s, Store(s.db_path), context=args.context or "")
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0 if not result.get("error") else 1
    if result.get("error"):
        print("error:", result["error"])
    r = result.get("reading") or {}
    print(f"{r.get('title')}  yes {r.get('yes_bid')}/{r.get('yes_ask')}c  no {r.get('no_bid')}/{r.get('no_ask')}c")
    a = result.get("assessment")
    if a:
        print(f"research: P({a['player_a']})={a['p_a_wins']:.2f} conf {a['confidence']:.2f} · {a['trade_view']}")
    for v in result.get("verdicts", []):
        line = f"  {v['player']:24s} {v['verdict']:16s} ask {v['ask']}c  form {v['form']:.0f}  comeback {v['comeback']:.0f}  P(win) {v['p_pre']:.2f}"
        if v["sell_target"]:
            line += f"  -> sell {v['sell_target']}c stop {v['stop']}c"
        if v["blockers"]:
            line += "  (" + "; ".join(v["blockers"]) + ")"
        print(line)
    return 0 if not result.get("error") else 1


def cmd_analyze(args, s: Settings) -> int:
    """Run the Claude vision analyst on a screenshot and score it with the risk engine."""
    from kalshitrader.analysis.estimators import Estimate
    from kalshitrader.analysis.vision import ClaudeAnalyst
    from kalshitrader.risk.rules import PortfolioState, RiskManager
    from kalshitrader.tracking.db import Store

    analyst = ClaudeAnalyst(model=s.anthropic_model, effort=s.claude_effort, api_key=s.anthropic_api_key)
    analysis = analyst.analyze_screenshot(args.image, extra_context=args.context or "")
    if analysis is None:
        print("analyst returned no reading (refusal or empty response)")
        return 1
    market = analysis.to_market()
    est = Estimate(p_yes=analysis.p_true, confidence=analysis.confidence, source="claude-vision", rationale=analysis.rationale)
    store = Store(s.db_path)
    state = PortfolioState(cash_dollars=s.paper_starting_cash, exposure_dollars=0, open_positions=0, daily_pnl_dollars=0, consecutive_losses=0)
    risk = RiskManager(s)
    risk.s.min_volume_24h = 0  # screenshots rarely show 24h volume
    sig = risk.build_signal(market, None, est, state)
    if args.json:
        print(json.dumps({"analysis": analysis.model_dump(), "signal": sig.to_record()}, indent=2, default=str))
    else:
        print(sig.format())
    store.add_signal(sig.to_record())
    return 0


def cmd_diagnose(args, s: Settings) -> int:
    """Show exactly what Kalshi returns for the tennis series, raw JSON included.

    This is the command to run when the dashboard shows no data: it prints the
    per-series counts, the raw fields of one market, and its order book.
    """
    from kalshitrader.markets.contest import find_contests

    client = _client(s)
    print(f"exchange   {s.base_url}")
    print(f"credentials {'present' if client.authenticated else 'none (market data only)'}")
    all_markets = []
    for series in s.enabled_series:
        try:
            batch = client.get_markets(limit=500, series_ticker=series, max_pages=3)
        except Exception as exc:
            print(f"  {series:28s} ERROR {exc}")
            continue
        statuses = {}
        quoted = 0
        for m in batch:
            statuses[m.status] = statuses.get(m.status, 0) + 1
            quoted += 1 if m.has_quote else 0
        print(f"  {series:28s} {len(batch):4d} markets  statuses={statuses}  with quotes={quoted}")
        all_markets.extend(batch)
    matches = find_contests(all_markets, s.enabled_series, s.series_prefixes)
    print(f"\nparsed {len(matches)} matches from {len(all_markets)} markets")

    # Sample the tape twice so we can say which matches are actually trading now,
    # which is what the bot means by "live" - the scheduled start is only a hint.
    import time as _time
    from datetime import datetime, timezone

    print(f"sampling volume over {args.sample}s to find matches that are trading...")
    before = {m.ticker: m.volume for m in all_markets}
    _time.sleep(args.sample)
    after: dict[str, int] = {}
    for series in s.enabled_series:
        try:
            for m in client.get_markets(limit=500, series_ticker=series, max_pages=3):
                after[m.ticker] = m.volume
        except Exception:
            continue
    now = datetime.now(timezone.utc)
    rows = []
    for match in matches:
        traded = sum(max(0, after.get(m.ticker, 0) - before.get(m.ticker, 0)) for m in match.markets)
        starts = next((m.starts_at for m in match.markets if m.starts_at), None)
        scheduled = starts is None or starts <= now
        rows.append((traded, scheduled, starts, match))
    rows.sort(key=lambda r: (-r[0], not r[1]))
    trading = sum(1 for r in rows if r[0] > 0)
    print(f"{trading} of {len(matches)} matches traded during the sample\n")
    for traded, scheduled, starts, match in rows[: args.limit]:
        when = "started" if scheduled else f"starts {starts.strftime('%d %b %H:%MZ')}"
        legs = "  ".join(f"{p}: {leg.bid}/{leg.ask}c" for p, leg in match.legs.items())
        flag = "TRADING" if traded > 0 else "quiet  "
        print(f"  {flag} {traded:6d}  {match.title[:38]:38s} {when:20s} {legs}")
    if trading == 0:
        print("\n  Nothing traded in the sample. Either no match is in play, or try a longer")
        print("  --sample. The bot treats a match as live when contracts actually change hands.")

    sample = None
    for m in all_markets:
        if m.is_tradeable:
            sample = m
            break
    sample = sample or (all_markets[0] if all_markets else None)
    if sample is None:
        print("\nNo markets returned. If this is the demo exchange, switch to prod: tennis series are not on demo.")
        return 1
    print(f"\nraw market JSON for {sample.ticker}:")
    print(json.dumps(sample.raw, indent=2, default=str)[: args.chars])
    try:
        raw = client._request("GET", f"/markets/{sample.ticker}/orderbook", params={"depth": 5})
        print(f"\nraw order book JSON for {sample.ticker}:")
        print(json.dumps(raw, indent=2, default=str)[: args.chars])
        book = client.get_orderbook(sample.ticker)
        print(f"parsed: yes={book.yes[:5]} no={book.no[:5]}")
        print(f"derived: yes bid {book.best_yes_bid} ask {book.best_yes_ask} | no bid {book.best_no_bid} ask {book.best_no_ask}")
    except Exception as exc:
        print(f"\norder book fetch failed: {exc}")
    return 0


def cmd_form(args, s: Settings) -> int:
    """Check the open tennis data sources and look a player up."""
    from pathlib import Path as _Path

    from kalshitrader.tennis.form import FormAnalyst, FormStore

    store = FormStore(cache_dir=str(_Path(s.db_path).parent / "form"), cache_hours=0 if args.refresh else s.form_cache_hours,
                      sources=tuple(s.form_sources))
    if not store.sources:
        print("No form sources configured, so player form is off and the bot trades price action alone.")
        print("There is no default source: the URLs this shipped with pointed at a repository")
        print("that does not exist. To use form data, put a CSV dataset in .env as URL")
        print("templates containing {year}, then set FORM_ENABLED=true:\n")
        print("  FORM_SOURCES=https://example.com/atp_{year}.csv,https://example.com/wta_{year}.csv\n")
        print("The CSV needs winner_name, loser_name, tourney_date, surface, score and the")
        print("two rank_points columns.")
        return 1
    rows = store.load(force=args.refresh)
    print(f"{rows} matches, {len(store.players)} players\n")
    for r in store.report:
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"  {mark} {r['rows']:6d}  {r['url'].rsplit('/', 1)[-1]:38s} {r['note']}")
    if not any(r["ok"] for r in store.report):
        print("\nNo source reachable. The bot will trade price swings only, which is fine -")
        print("form data is an input, not a requirement. Check your network or a proxy.")
        return 1
    for name in args.player or []:
        rec = store.find(name)
        if rec is None:
            print(f"\n{name}: not found in the data")
            continue
        analyst = FormAnalyst(store=store)
        print(f"\n{rec.name}: {rec.played} matches on file")
        print(f"  recent form   {analyst._pct(rec.win_rate())}")
        print(f"  deciding sets {analyst._pct(rec.decider_win_rate())}")
        print(f"  ranking pts   {rec.rank_points()}")
        for m in rec.recent(5):
            print(f"    {m['date']}  {'W' if m['won'] else 'L'}  vs {m['opponent']} ({m['surface']})")
    return 0


def cmd_estimate(args, s: Settings) -> int:
    from kalshitrader.analysis.estimators import ManualEstimator

    me = ManualEstimator(s.manual_estimates_path)
    if args.remove:
        print("removed" if me.remove(args.ticker) else "no such estimate")
        return 0
    if args.p_yes is None:
        print("p_yes is required unless --remove is given", file=sys.stderr)
        return 2
    me.set(args.ticker, args.p_yes, args.confidence, args.note or "")
    print(f"{args.ticker.upper()}: P(yes)={args.p_yes:.2f} conf={args.confidence:.2f}")
    return 0


def cmd_estimates(args, s: Settings) -> int:
    from kalshitrader.analysis.estimators import ManualEstimator

    data = ManualEstimator(s.manual_estimates_path).all()
    if not data:
        print("no manual estimates")
    for k, v in sorted(data.items()):
        print(f"{k:32s} P(yes)={v['p_yes']:.2f} conf={v.get('confidence', 0):.2f} {v.get('note', '')}")
    return 0


def cmd_report(args, s: Settings) -> int:
    from kalshitrader.tracking.db import Store
    from kalshitrader.tracking.metrics import compute_metrics

    store = Store(s.db_path)
    m = compute_metrics(store.all_trades(limit=100000), store.equity_curve(limit=100000), store.get_state("paper_starting_cash"))
    if args.json:
        print(json.dumps(m, indent=2, default=str))
        return 0
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    print(f"mode={s.trading_mode} env={s.kalshi_env} halted={bool(store.get_state('halted', False))}")
    print(f"equity        ${m['equity']:.2f}  (cash ${m['cash']:.2f}, return {m['total_return'] * 100:+.1f}%)")
    print(f"realized P&L  ${m['realized_pnl']:+.2f}  today ${m['daily_pnl']:+.2f}  7d ${m['week_pnl']:+.2f}  unrealized ${m['unrealized_pnl']:+.2f}")
    print(f"trades        {m['trades_closed']} closed / {m['trades_open']} open  win rate {m['win_rate'] * 100:.1f}%  ({m['wins']}W {m['losses']}L)")
    print(f"avg win ${m['avg_win']:.2f}  avg loss ${m['avg_loss']:.2f}  profit factor {pf}  expectancy ${m['expectancy']:+.2f}")
    print(f"max drawdown  ${m['max_drawdown']:.2f} ({m['max_drawdown_pct'] * 100:.1f}%)  sharpe {m['sharpe']:.2f}  fees ${m['total_fees']:.2f}")
    for t in store.open_trades():
        print(f"  OPEN {t['ticker']} {t['side'].upper()} x{t['count']} @ {t['entry_price']}c TP {t['take_profit']}c SL {t['stop_loss']}c")
    return 0


def cmd_halt(args, s: Settings) -> int:
    from kalshitrader.tracking.db import Store

    Store(s.db_path).set_state("halted", not args.resume)
    print("resumed" if args.resume else "halted: no new entries until `kalshitrader halt --resume`")
    return 0


def cmd_markets(args, s: Settings) -> int:
    client = _client(s)
    markets = client.get_markets(limit=args.limit, series_ticker=args.series, event_ticker=args.event)
    markets.sort(key=lambda m: -m.volume_24h)
    print(f"{'TICKER':34s} {'BID':>4s} {'ASK':>4s} {'SPR':>4s} {'VOL24':>7s} {'CLOSE':>17s}  TITLE")
    for m in markets[: args.limit]:
        close = m.close_time.strftime("%Y-%m-%d %H:%MZ") if m.close_time else "-"
        print(f"{m.ticker:34s} {m.yes_bid:4d} {m.yes_ask:4d} {m.yes_spread:4d} {m.volume_24h:7d} {close:>17s}  {m.title[:60]}")
    return 0


def cmd_discover(args, s: Settings) -> int:
    """Sweep the exchange and report every series trading right now, ranked."""
    from datetime import datetime, timezone

    from kalshitrader.markets.discovery import by_category, sweep
    from kalshitrader.tracking.db import Store

    client = _client(s)
    series = sweep(client, pages=args.pages, limit=1000)
    if not series:
        print("No open markets came back. Check your keys and that KALSHI_ENV=prod.")
        return 1
    Store(s.db_path).set_state("series_discovered", [x.as_dict() for x in series])
    Store(s.db_path).set_state("series_discovered_at", datetime.now(timezone.utc).isoformat())
    enabled = {x.upper() for x in s.enabled_series}
    shown = 0
    for category, rows in by_category(series).items():
        keep = [r for r in rows if r.volume_24h >= args.min_volume or r.ticker.upper() in enabled]
        if not keep:
            continue
        print(f"\n{category}")
        for r in keep[: args.per_category]:
            mark = "on " if r.ticker.upper() in enabled else "   "
            # Liquid markets, not total markets: a series full of one-sided books is
            # untradeable however many contracts it lists.
            print(f"  {mark}{r.ticker:28s} {r.volume_24h:>10,} vol  {r.liquid_markets:>3}/{r.open_markets:<3} liquid  {r.examples[0][:44] if r.examples else ''}")
            shown += 1
    print(f"\n{shown} series shown of {len(series)} found. Switch them on in the dashboard under Markets,")
    print("or set ENABLED_SERIES in .env. Volume is 24h across the whole series.")
    return 0


def cmd_backtest(args, s: Settings) -> int:
    """Replay recorded prices through the strategy, with one or many configurations."""
    from dataclasses import replace

    from kalshitrader.backtest import run_backtest, sweep_settings
    from kalshitrader.tracking.db import Store

    store = Store(s.db_path)
    first, last, rows = store.snapshot_span()
    if not rows:
        print("No price history recorded yet. The bot writes a snapshot for every market it")
        print("watches on every scan, so run it for a while first - even paused, it records.")
        return 1
    print(f"{rows:,} snapshots from {first} to {last}\n")

    if args.sweep:
        # One axis at a time: a grid over every knob invites reading noise as a result.
        variations = [{"dip_cents": d} for d in (6, 8, 10, 12, 14, 18, 24, 30)] if args.sweep == "dip" \
            else [{"stop_loss_cents": v, "use_stop_loss": v > 0} for v in (0, 4, 6, 8, 12, 20)] if args.sweep == "stop" \
            else [{"take_profit_cents": v} for v in (4, 6, 8, 10, 14, 20)]
        results = sweep_settings(store, s, variations, since=args.since)
        label = {"dip": "dip", "stop": "stop", "target": "target"}[args.sweep]
        print(f"{label:>7s} {'closed':>7s} {'open':>5s} {'win%':>6s} {'break-even':>11s} {'P&L':>9s} {'per trade':>10s} {'fees':>8s}")
        unfinished = False
        for changes, r in zip(variations, results, strict=True):
            value = list(changes.values())[0]
            be = f"{r.break_even_win_rate * 100:.0f}%" if r.break_even_win_rate is not None else "-"
            unfinished = unfinished or r.open_at_end > len(r.closed)
            print(f"{value:>7} {len(r.closed):>7d} {r.open_at_end:>5d} {r.win_rate * 100:>5.0f}% {be:>11s} "
                  f"{r.pnl:>+9.2f} {r.expectancy:>+10.2f} {r.fees:>8.2f}")
        print("\nBreak-even is the win rate that configuration needed to stand still, from what")
        print("its own trades returned. Beat it and it made money; miss it and it did not.")
        if unfinished:
            print("\nSome rows left more positions open than they closed. Those P&L figures count")
            print("only what finished, so they flatter a configuration that simply holds losers -")
            print("read them alongside the open column, or replay a longer history.")
        return 0

    settings = replace(s, **{k: v for k, v in (
        ("dip_cents", args.dip), ("take_profit_cents", args.target),
        ("stop_loss_cents", args.stop), ("min_edge_cents", args.edge)) if v is not None})
    if args.no_stop:
        settings = replace(settings, use_stop_loss=False)
    if args.compare_execution:
        from kalshitrader.backtest import load_history

        history = load_history(store, since=args.since)
        taker = run_backtest(store, settings, history=history)
        maker = run_backtest(store, settings, history=history, maker=True)
        print(f"{'':10s} {'closed':>7s} {'open':>5s} {'win%':>6s} {'P&L':>9s} {'per trade':>10s} {'fees':>8s} {'fill rate':>10s}")
        for name, r in (("taker", taker), ("maker", maker)):
            fr = f"{r.fill_rate * 100:.0f}%" if r.fill_rate is not None else "-"
            print(f"{name:10s} {len(r.closed):>7d} {r.open_at_end:>5d} {r.win_rate * 100:>5.0f}% "
                  f"{r.pnl:>+9.2f} {r.expectancy:>+10.2f} {r.fees:>8.2f} {fr:>10s}")
        if maker.posted:
            print(f"\nmaker posted {maker.posted} orders, {maker.unfilled} expired unfilled.")
        print("\nTaker crosses the spread on both sides and always fills. Maker posts at the bid")
        print("and sells at the ask, saving the spread, but only fills when someone trades")
        print("against it - and the fill model cannot see the queue ahead of you, so treat the")
        print("fill rate as a ceiling.")
        return 0

    r = run_backtest(store, settings, since=args.since, maker=args.maker)
    print(f"settings   {r.settings}{' (maker)' if args.maker else ''}")
    print(f"replayed   {r.tickers} tickers, {r.snapshots:,} snapshots, {r.span_hours:.1f}h\n")
    if not r.closed:
        print(f"No trades closed{' (' + str(r.open_at_end) + ' still open at the end)' if r.open_at_end else ''}.")
        print("Nothing in this history met the entry rules - loosen the dip, or record more.")
        return 0
    be = f"{r.break_even_win_rate * 100:.0f}%" if r.break_even_win_rate is not None else "n/a"
    pf = f"{r.profit_factor:.2f}" if r.profit_factor is not None else "inf"
    print(f"trades     {len(r.closed)} closed, {r.open_at_end} open at the end")
    print(f"win rate   {r.win_rate * 100:.0f}%   (needed {be} to break even)")
    print(f"P&L        {r.pnl:+.2f}   per trade {r.expectancy:+.2f}   profit factor {pf}")
    print(f"fees       {r.fees:.2f}   ({r.fees / abs(r.pnl) * 100:.0f}% the size of the P&L)" if r.pnl else f"fees       {r.fees:.2f}")
    if args.trades:
        print(f"\n{'ticker':34s} {'in':>4s} {'out':>4s} {'qty':>5s} {'P&L':>8s}  why")
        for t in r.closed[: args.trades]:
            print(f"{t['ticker'][:34]:34s} {t['entry_price']:>4d} {t['exit_price']:>4d} "
                  f"{t['count']:>5d} {t['pnl']:>+8.2f}  {t['exit_reason']}")
    return 0


def cmd_balance(args, s: Settings) -> int:
    client = _client(s)
    if not client.authenticated:
        print("no credentials configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH)")
        return 1
    bal = client.get_balance()
    print(f"balance ${bal.get('balance', 0) / 100:.2f}  portfolio value ${bal.get('portfolio_value', 0) / 100:.2f}")
    for p in client.get_positions():
        print(f"  {p.ticker} {p.side.upper()} x{abs(p.contracts)} exposure ${p.market_exposure_cents / 100:.2f}")
    return 0


# --------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshitrader", description="Cold, low-risk Kalshi trading agent")
    p.add_argument("--env-file", default=".env", help="path to .env (default ./.env)")
    p.add_argument("--version", action="version", version=f"kalshitrader {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="one read-only pass: print signals, place nothing")
    sp.add_argument("--all", action="store_true", help="show PASS signals too")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_scan)

    rp = sub.add_parser("run", help="run the trading loop")
    rp.add_argument("--once", action="store_true", help="one cycle then exit")
    rp.add_argument("--interval", type=int, default=None, help="seconds between cycles")
    rp.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    rp.add_argument("--yes", action="store_true", help="skip the LIVE confirmation prompt")
    rp.set_defaults(fn=cmd_run)

    dp = sub.add_parser("dashboard", help="serve the web dashboard")
    dp.add_argument("--host", default=None)
    dp.add_argument("--port", type=int, default=None)
    dp.set_defaults(fn=cmd_dashboard)

    ap = sub.add_parser("analyze", help="analyze a Kalshi screenshot with Claude and score it")
    ap.add_argument("image")
    ap.add_argument("--context", default=None, help="extra context for the analyst")
    ap.add_argument("--json", action="store_true")
    ap.set_defaults(fn=cmd_analyze)

    tp = sub.add_parser("tennis-analyze", help="research a tennis screenshot: form, matchup, buyable on a dip?")
    tp.add_argument("image")
    tp.add_argument("--context", default=None)
    tp.add_argument("--json", action="store_true")
    tp.set_defaults(fn=cmd_tennis_analyze)

    dg = sub.add_parser("diagnose", help="show what Kalshi returns for the tennis series (run this if the dashboard is empty)")
    dg.add_argument("--limit", type=int, default=25, help="matches to list")
    dg.add_argument("--chars", type=int, default=2500, help="characters of raw JSON to print")
    dg.add_argument("--sample", type=int, default=20, help="seconds to watch volume for, to find matches in play")
    dg.set_defaults(fn=cmd_diagnose)

    fp = sub.add_parser("form", help="check the open tennis data sources and look players up")
    fp.add_argument("player", nargs="*", help="player names to report on")
    fp.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    fp.set_defaults(fn=cmd_form)

    ep = sub.add_parser("estimate", help="set your own P(yes) for a ticker")
    ep.add_argument("ticker")
    ep.add_argument("p_yes", type=float, nargs="?", default=None)
    ep.add_argument("--confidence", type=float, default=0.8)
    ep.add_argument("--note", default=None)
    ep.add_argument("--remove", action="store_true")
    ep.set_defaults(fn=cmd_estimate)

    sub.add_parser("estimates", help="list manual estimates").set_defaults(fn=cmd_estimates)

    rpt = sub.add_parser("report", help="print performance metrics")
    rpt.add_argument("--json", action="store_true")
    rpt.set_defaults(fn=cmd_report)

    hp = sub.add_parser("halt", help="stop new entries (exits still run)")
    hp.add_argument("--resume", action="store_true")
    hp.set_defaults(fn=cmd_halt)

    dp = sub.add_parser("discover", help="find every Kalshi series trading now, ranked by volume")
    dp.add_argument("--pages", type=int, default=12, help="pages of 1000 markets to sweep")
    dp.add_argument("--min-volume", type=int, default=1000, help="hide series quieter than this")
    dp.add_argument("--per-category", type=int, default=8)
    dp.set_defaults(fn=cmd_discover)

    bt = sub.add_parser("backtest", help="replay recorded prices through the strategy")
    bt.add_argument("--dip", type=int, default=None, help="override the dip required to buy")
    bt.add_argument("--target", type=int, default=None, help="override the take-profit")
    bt.add_argument("--stop", type=int, default=None, help="override the stop loss")
    bt.add_argument("--edge", type=float, default=None, help="override the minimum edge")
    bt.add_argument("--no-stop", action="store_true", help="ride positions to target or settlement")
    bt.add_argument("--since", default=None, help="ISO timestamp; replay only from here")
    bt.add_argument("--sweep", choices=["dip", "stop", "target"], help="compare a range of one setting")
    bt.add_argument("--maker", action="store_true",
                    help="post at the bid and sell at the ask instead of crossing the spread")
    bt.add_argument("--compare-execution", action="store_true",
                    help="run the same history both ways and show what crossing the spread costs")
    bt.add_argument("--trades", type=int, default=0, help="also print this many individual trades")
    bt.set_defaults(fn=cmd_backtest)

    mp = sub.add_parser("markets", help="list open markets")
    mp.add_argument("--series", default=None)
    mp.add_argument("--event", default=None)
    mp.add_argument("--limit", type=int, default=30)
    mp.set_defaults(fn=cmd_markets)

    sub.add_parser("balance", help="show Kalshi balance and positions").set_defaults(fn=cmd_balance)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_level)
    sys.exit(args.fn(args, settings))


if __name__ == "__main__":
    main()
