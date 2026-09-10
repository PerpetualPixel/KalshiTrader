"""Tennis engine: the general Engine with match discovery, research, and the swing strategy."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshitrader.analysis.blockers import ranked as ranked_blockers
from kalshitrader.analysis.blockers import tally as tally_blocker
from kalshitrader.config import Settings, apply_overrides, load_settings
from kalshitrader.execution.base import Broker
from kalshitrader.kalshi.client import KalshiClient, KalshiError
from kalshitrader.kalshi.models import Market
from kalshitrader.markets.contest import Contest, find_contests
from kalshitrader.strategy.engine import CycleResult, Engine
from kalshitrader.tennis.analyst import TennisAnalyst
from kalshitrader.tennis.form import FormAnalyst, FormStore
from kalshitrader.tracking.db import Store
from kalshitrader.trading.strategy import SwingStrategy

log = logging.getLogger(__name__)


class TennisEngine(Engine):
    def __init__(self, settings: Settings, client: KalshiClient, store: Store, broker: Broker,
                 analyst: TennisAnalyst | None = None, env_file: str | None = ".env"):
        super().__init__(settings, client, store, broker, estimator=None)
        self.analyst = analyst or TennisAnalyst(
            store, model=settings.anthropic_model, effort=settings.claude_effort, ttl_minutes=settings.assessment_ttl_minutes,
            max_searches=settings.research_max_searches, enabled=settings.research_enabled,
            api_key=settings.anthropic_api_key,
        )
        # Player form from open data: free, fast enough to run on the loop, and the
        # default source. Claude research is an optional extra on top.
        self.form = FormAnalyst(
            store=FormStore(cache_dir=str(Path(settings.db_path).parent / "form"), cache_hours=settings.form_cache_hours,
                            sources=tuple(settings.form_sources)),
            enabled=settings.form_enabled, min_matches=settings.form_min_matches,
        )
        self.strategy = SwingStrategy(settings, store, self.risk)
        self.orderbook_budget = 120
        self.env_file = env_file
        self._env_mtime = self._mtime()
        self.matches: list[Contest] = []

    # ------------------------------------------------------------ settings
    def _mtime(self) -> float:
        try:
            import os

            return os.path.getmtime(self.env_file) if self.env_file else 0.0
        except OSError:
            return 0.0

    def refresh_settings(self) -> None:
        """Apply dashboard overrides; reload .env (and credentials) if it changed on disk."""
        mtime = self._mtime()
        if self.env_file and mtime != self._env_mtime:
            self._env_mtime = mtime
            try:
                fresh = load_settings(self.env_file)
            except ValueError as exc:
                log.warning(".env changed but is invalid: %s", exc)
            else:
                creds_changed = (fresh.kalshi_api_key_id, fresh.kalshi_private_key_pem, fresh.kalshi_private_key_path, fresh.base_url) != (
                    self.s.kalshi_api_key_id, self.s.kalshi_private_key_pem, self.s.kalshi_private_key_path, self.s.base_url)
                mode_changed = fresh.trading_mode != self.s.trading_mode
                for f in fresh.__dataclass_fields__:
                    setattr(self.s, f, getattr(fresh, f))
                if creds_changed or mode_changed:
                    try:
                        self.client = KalshiClient(self.s.base_url, api_key_id=self.s.kalshi_api_key_id,
                                                   private_key_path=self.s.kalshi_private_key_path, private_key_pem=self.s.kalshi_private_key_pem)
                        if hasattr(self.broker, "client"):
                            self.broker.client = self.client  # type: ignore[attr-defined]
                        if mode_changed:
                            self._switch_broker()
                        log.info("reloaded Kalshi credentials/mode from .env (mode=%s)", self.mode)
                    except Exception as exc:
                        log.error("could not rebuild Kalshi client from new .env: %s", exc)
                if fresh.anthropic_api_key != self.analyst.api_key:
                    # New key saved from the dashboard: rebuild and forget past failures.
                    self.analyst.api_key = fresh.anthropic_api_key
                    self.analyst.reset_errors()
                    log.info("Anthropic key changed; research will retry")
                self.analyst.model = self.s.anthropic_model
        apply_overrides(self.s, self.store.get_state("settings_overrides", {}))
        self.analyst.enabled = self.s.research_enabled
        self.analyst.ttl = self.s.assessment_ttl_minutes
        self.analyst.max_searches = self.s.research_max_searches

    def _switch_broker(self) -> None:
        from kalshitrader.execution.live import LiveBroker
        from kalshitrader.execution.paper import PaperBroker

        if self.s.is_live:
            if not self.client.authenticated:
                log.error("TRADING_MODE=live but no Kalshi credentials; staying in %s mode", self.mode)
                return
            self.broker = LiveBroker(self.client, self.store, fee_rate=self.s.fee_rate)
        else:
            self.broker = PaperBroker(self.store, self.s.paper_starting_cash, self.s.paper_slippage_cents, self.s.fee_rate)
        self.mode = self.broker.mode
        log.warning("execution switched to %s", self.mode.upper())

    # -------------------------------------------------------------- markets
    def fetch_markets(self) -> list[Market]:
        """Pull the configured tennis series directly; fall back to paging all open markets."""
        diagnostics: dict[str, str] = {"exchange": self.s.base_url}
        if self.s.enabled_series:
            out: list[Market] = []
            seen: set[str] = set()
            for series in self.s.enabled_series:
                try:
                    batch = self.client.get_markets(limit=self.s.market_scan_limit, series_ticker=series, max_pages=5)
                except KalshiError as exc:
                    diagnostics[series] = f"error {exc.status}: {str(exc)[:80]}"
                    if exc.status == 404:
                        log.warning("series %s not found on %s", series, self.s.base_url)
                        continue
                    self.store.set_state("tennis_discovery", diagnostics)
                    raise
                diagnostics[series] = f"{len(batch)} open markets"
                for m in batch:
                    if m.ticker not in seen:
                        seen.add(m.ticker)
                        out.append(m)
            self.store.set_state("tennis_discovery", diagnostics)
            return out
        markets = self.client.get_markets(limit=self.s.market_scan_limit, max_pages=10)
        diagnostics["all"] = f"{len(markets)} open markets scanned by prefix"
        self.store.set_state("tennis_discovery", diagnostics)
        return markets

    def discover(self, markets: list[Market] | None = None) -> list[Contest]:
        """Group the markets we are allowed to trade into contests."""
        markets = markets if markets is not None else self.fetch_markets()
        self.matches = find_contests(markets, self.s.enabled_series, self.s.series_prefixes)
        return self.matches

    # ----------------------------------------------------------------- scan
    def scan(self, execute: bool = True, markets: list[Market] | None = None) -> CycleResult:
        result = CycleResult()
        now = datetime.now(timezone.utc)
        matches = self.discover(markets)
        result.markets_scanned = sum(len(m.markets) for m in matches)
        state = self.portfolio_state()
        watch: list[dict] = []
        blockers: dict[str, dict] = {}
        # Tradeable matches first, then by turnover: the order-book budget should be
        # spent on markets we could actually trade.
        matches.sort(key=lambda m: (not m.is_open(), -m.volume_24h))
        books_left = self.orderbook_budget
        for match in matches:
            assessment = self.analyst.cached(match) or self._form_assessment(match)
            if not match.is_open():
                watch.append(self._watch_row(match, assessment, [], False, "closed"))
                continue
            # The /markets list response does not always carry live quotes, so pull the
            # order book before deciding anything - liveness included.
            books: dict[str, object] = {}
            for leg in match.legs.values():
                if leg.ticker in books:
                    leg.attach(books[leg.ticker])
                    continue
                book = self._book(leg.ticker) if books_left > 0 else None
                books_left -= 1 if books_left > 0 else 0
                books[leg.ticker] = book
                leg.attach(book)
            for m in match.markets:
                self.store.add_snapshot(_snapshot_dict(m, match))
            live_any = self.strategy.match_live(match, now)
            if assessment is None:
                assessment = self._form_assessment(match)
            if assessment is None and self.s.research_enabled and (live_any or not self.s.live_only):
                # Queues the work and returns immediately: research must not stall the
                # loop, or exits go unchecked while Claude reads the web.
                assessment = self.analyst.request(match)
            signals = self.strategy.evaluate(match, assessment, books, state, now)
            result.signals.extend(signals)
            watch.append(self._watch_row(match, assessment, signals, live_any, "live" if live_any else "pre-match", now))
            for sig in signals:
                if not sig.is_trade:
                    tally_blocker(blockers, sig.rationale, sig.ticker)
                    if sig.ev_net > 0 or "dip" in sig.rationale:
                        self.store.add_signal(sig.to_record())
                    continue
                signal_id = self.store.add_signal(sig.to_record())
                log.info("\n%s", sig.format())
                if execute and self.execute(sig, signal_id):
                    result.trades_opened += 1
                    state = self.portfolio_state()
        order = {"live": 0, "pre-match": 1, "closed": 2}
        watch.sort(key=lambda r: (order.get(r["status"], 3), -(r["volume_24h"] or 0)))
        self.store.set_state("tennis_watch", watch)
        self.form.enabled = self.s.form_enabled
        self.store.set_state("research_status", self.analyst.status if self.s.research_enabled else self.form.status)
        self.store.set_state("form_status", self.form.status)
        self.store.set_state("research_pending", getattr(self.analyst, "pending", 0))
        self.store.set_state("tennis_blockers", ranked_blockers(blockers))
        self.store.set_state("tennis_counts", {
            "matches": len(matches), "live": sum(1 for r in watch if r["status"] == "live"),
            "researched": sum(1 for r in watch if r["researched"]),
        })
        return result

    def _form_assessment(self, match: Contest):
        """Open-data form for this match, or None when neither player is in the files."""
        if not self.s.form_enabled:
            return None
        try:
            return self.form.assess(match)
        except Exception as exc:  # form data must never take the loop down
            log.warning("form lookup failed for %s: %s", match.title, str(exc)[:160])
            return None

    def _watch_row(self, match: Contest, assessment, signals, live: bool, status: str, now=None) -> dict:
        legs = []
        # Contracts traded across the match inside the live window: the evidence the
        # bot uses to call a match live, shown so the dashboard explains itself.
        traded = max((self.strategy.price_context(leg, now).volume_delta for leg in match.legs.values()), default=0)
        for player, leg in match.legs.items():
            p_pre, pa = assessment.for_player(player) if assessment else (None, None)
            sig = next((s for s in signals if s.ticker == leg.ticker and s.title.endswith(player)), None)
            legs.append({
                "player": player, "ticker": leg.ticker, "side": leg.side, "bid": leg.bid, "ask": leg.ask,
                "p_pre": p_pre, "form": pa.form_score if pa else None, "comeback": pa.comeback_score if pa else None,
                "decision": (sig.action.value if sig else "-"), "reason": (sig.rationale[:120] if sig else ""),
            })
        return {
            "key": match.key, "title": match.title, "tournament": match.tournament, "live": live, "status": status,
            "close_time": match.close_time.isoformat() if match.close_time else None,
            "researched": assessment is not None, "confidence": assessment.confidence if assessment else None,
            "trade_view": assessment.trade_view if assessment else "", "legs": legs, "volume_24h": match.volume_24h,
            "traded": traded,
        }

    # ----------------------------------------------------------- reconciliation
    def reconcile_positions(self) -> int:
        """Close local trades the exchange no longer holds.

        Kalshi settles winners and losers into cash without sending a fill, so a
        settled match can otherwise sit in our ledger forever as open exposure.
        Only meaningful in live mode; paper positions are ours alone.
        """
        if not self.s.is_live or not self.client.authenticated:
            return 0
        try:
            positions = {p.ticker: p for p in self.client.get_positions()}
        except KalshiError as exc:
            log.warning("could not read positions for reconciliation: %s", exc)
            return 0
        closed = 0
        for t in self.store.open_trades(self.mode):
            held = positions.get(t["ticker"])
            if held is not None and abs(held.contracts) >= t["count"]:
                continue
            try:
                market = self.client.get_market(t["ticker"])
            except KalshiError:
                continue
            if market.is_settled():
                won = market.result == t["side"]
                self.store.close_trade(t["id"], exit_price=100 if won else 0,
                                       exit_reason=f"settled {market.result} (reconciled)", status="settled")
            elif held is None:
                # Gone from the exchange but not settled: mark it closed at the last
                # bid we can see rather than leaving phantom exposure on the books.
                bid = market.yes_bid if t["side"] == "yes" else market.no_bid
                self.store.close_trade(t["id"], exit_price=bid or t["entry_price"], exit_reason="closed on exchange (reconciled)")
            else:
                continue
            closed += 1
            log.warning("reconciled %s: local ledger disagreed with the exchange", t["ticker"])
        return closed

    # ---------------------------------------------------------------- cycle
    def cycle(self, execute: bool = True) -> CycleResult:
        self.refresh_settings()
        result = super().cycle(execute=execute)
        try:
            result.trades_closed += self.reconcile_positions()
        except Exception as exc:  # never let an audit kill the loop
            log.warning("reconciliation failed: %s", exc)
        return result

    def run_forever(self, interval: int | None = None, max_cycles: int | None = None) -> None:
        n = 0
        log.info("KalshiTrader tennis running: mode=%s env=%s research=%s", self.mode, self.s.kalshi_env, self.analyst.status)
        while True:
            try:
                res = self.cycle()
                # When nothing traded, say what stopped it in the same line. The console
                # is where this is actually watched, and "0 entries" on its own has sent
                # people hunting for a network fault that was really a settings gate.
                blocked = ""
                if not res.trades_opened:
                    top = (self.store.get_state("tennis_blockers", []) or [None])[0]
                    if top:
                        blocked = f" · mostly: {top['reason']} ({top['count']})"
                log.info("cycle: %d matches, %d markets, %d entries, %d exits, %dms%s%s", len(self.matches), res.markets_scanned,
                         res.trades_opened, res.trades_closed, res.duration_ms, blocked,
                         f" ERROR {res.error}" if res.error else "")
                n += 1
                if max_cycles is not None and n >= max_cycles:
                    return
                time.sleep(max(1, (interval or self.s.scan_interval_seconds) - res.duration_ms / 1000))
            except KeyboardInterrupt:
                open_now = len(self.store.open_trades(self.mode))
                log.info("stopped. %d position(s) left open - they are NOT being managed while the bot is down.", open_now)
                return


# Every number stripped out, so "dip 3c < 14c (high 60c, ask 57c)" and the same
# sentence about another market collapse into one row. Answering "why did nothing
# trade?" needs the shape of the reason, not 76 copies of it.
def _snapshot_dict(m: Market, match: Contest | None = None) -> dict:
    """Snapshot row for a market, preferring order-book prices when the list response
    carried none (in-play markets often report 0 in /markets)."""
    yes_bid, yes_ask, no_bid, no_ask = m.yes_bid, m.yes_ask, m.no_bid, m.no_ask
    if match is not None and not m.has_quote:
        for leg in match.legs.values():
            if leg.ticker != m.ticker or leg.book is None:
                continue
            yes_bid = yes_bid or (leg.book.best_yes_bid or 0)
            yes_ask = yes_ask or (leg.book.best_yes_ask or 0)
            no_bid = no_bid or (leg.book.best_no_bid or 0)
            no_ask = no_ask or (leg.book.best_no_ask or 0)
    return {"ticker": m.ticker, "yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask,
            "last_price": m.last_price, "volume_24h": m.volume_24h, "open_interest": m.open_interest, "volume": m.volume}
