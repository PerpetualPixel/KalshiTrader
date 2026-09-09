"""P_true estimators.

An estimator looks at a market (and its order book) and returns a probability
that the market settles YES, with a confidence in [0, 1]. The engine combines
several estimators through `CompositeEstimator`.

Built in:
  * ManualEstimator         - probabilities you researched yourself, stored in a JSON file
                              (`kalshitrader estimate TICKER 0.62`). Highest signal, highest confidence.
  * MicrostructureEstimator - fair value from the order book: mid price, shrunk toward
                              50/50 as the spread widens, nudged by book imbalance. Low
                              confidence by design: it mostly keeps the bot honest and PASSing.
  * ClaudeMarketEstimator   - asks Claude for P_true from the contract text + book (optional,
                              requires the `anthropic` extra and ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kalshitrader.kalshi.models import Market, Orderbook

log = logging.getLogger(__name__)


@dataclass
class Estimate:
    p_yes: float
    confidence: float
    source: str
    rationale: str = ""

    def clamp(self) -> Estimate:
        self.p_yes = max(0.01, min(0.99, self.p_yes))
        self.confidence = max(0.0, min(1.0, self.confidence))
        return self


class Estimator(Protocol):
    name: str

    def estimate(self, market: Market, book: Orderbook | None) -> Estimate | None: ...


class ManualEstimator:
    name = "manual"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            log.warning("could not parse %s; ignoring manual estimates", self.path)
            return {}

    def set(self, ticker: str, p_yes: float, confidence: float = 0.8, note: str = "") -> None:
        data = self._load()
        data[ticker.upper()] = {"p_yes": float(p_yes), "confidence": float(confidence), "note": note}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def remove(self, ticker: str) -> bool:
        data = self._load()
        if ticker.upper() in data:
            del data[ticker.upper()]
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            return True
        return False

    def all(self) -> dict:
        return self._load()

    def estimate(self, market: Market, book: Orderbook | None) -> Estimate | None:
        entry = self._load().get(market.ticker.upper())
        if not entry:
            return None
        return Estimate(
            p_yes=float(entry["p_yes"]),
            confidence=float(entry.get("confidence", 0.8)),
            source=self.name,
            rationale=entry.get("note") or "manual research estimate",
        ).clamp()


class MicrostructureEstimator:
    """Order-book fair value. Deliberately timid.

    p = mid/100, shrunk toward 0.5 by spread, then tilted by depth imbalance.
    Confidence falls off quickly with spread and rises with volume, but is
    capped at `max_confidence` because a book alone rarely reveals an edge
    the book itself hasn't already priced.
    """

    name = "microstructure"

    def __init__(self, max_confidence: float = 0.45, imbalance_weight: float = 0.03):
        self.max_confidence = max_confidence
        self.imbalance_weight = imbalance_weight

    def estimate(self, market: Market, book: Orderbook | None) -> Estimate | None:
        if market.yes_ask <= 0 or market.yes_bid <= 0:
            return None
        spread = market.yes_spread
        mid = market.yes_mid / 100.0
        shrink = min(1.0, spread / 20.0)  # 20c spread -> fully shrunk to 0.5
        p = mid * (1 - shrink) + 0.5 * shrink
        tilt = 0.0
        if book is not None:
            tilt = book.imbalance() * self.imbalance_weight
            p += tilt
        volume_factor = min(1.0, market.volume_24h / 500.0)
        conf = self.max_confidence * max(0.0, 1 - spread / 10.0) * (0.5 + 0.5 * volume_factor)
        return Estimate(
            p_yes=p,
            confidence=conf,
            source=self.name,
            rationale=f"book mid {market.yes_mid:.1f}c, spread {spread}c, imbalance tilt {tilt:+.3f}",
        ).clamp()


class ClaudeMarketEstimator:
    """Ask Claude for P_true from the market's text and order book.

    Lazy-imports the analyst so the rest of the bot works without the `ai` extra.
    """

    name = "claude"

    def __init__(self, model: str = "claude-opus-5", effort: str = "high", client=None, api_key: str | None = None):
        from kalshitrader.analysis.vision import ClaudeAnalyst

        self.analyst = ClaudeAnalyst(model=model, effort=effort, client=client, api_key=api_key)

    def estimate(self, market: Market, book: Orderbook | None) -> Estimate | None:
        try:
            result = self.analyst.analyze_market(market, book)
        except Exception as exc:  # network / refusal / parse -> no opinion
            log.warning("claude estimator failed for %s: %s", market.ticker, exc)
            return None
        if result is None:
            return None
        return Estimate(
            p_yes=result.p_true, confidence=result.confidence, source=self.name, rationale=result.rationale
        ).clamp()


class CompositeEstimator:
    """Confidence-weighted blend of several estimators.

    Confidence of the blend is the max member confidence, damped when the
    members disagree by more than `disagreement_tolerance`.
    """

    name = "composite"

    def __init__(self, estimators: list[Estimator], disagreement_tolerance: float = 0.15):
        self.estimators = estimators
        self.disagreement_tolerance = disagreement_tolerance

    def estimate(self, market: Market, book: Orderbook | None) -> Estimate | None:
        parts: list[Estimate] = []
        for est in self.estimators:
            try:
                e = est.estimate(market, book)
            except Exception as exc:
                log.warning("estimator %s raised on %s: %s", getattr(est, "name", est), market.ticker, exc)
                continue
            if e is not None and e.confidence > 0:
                parts.append(e)
        if not parts:
            return None
        total_w = sum(e.confidence for e in parts)
        p = sum(e.p_yes * e.confidence for e in parts) / total_w
        conf = max(e.confidence for e in parts)
        spread_of_opinion = max(e.p_yes for e in parts) - min(e.p_yes for e in parts)
        if spread_of_opinion > self.disagreement_tolerance:
            conf *= max(0.2, 1 - (spread_of_opinion - self.disagreement_tolerance) * 2)
        sources = "+".join(e.source for e in parts)
        rationale = "; ".join(f"{e.source}: p={e.p_yes:.2f} c={e.confidence:.2f} ({e.rationale})" for e in parts)
        return Estimate(p_yes=p, confidence=conf, source=sources, rationale=rationale).clamp()


def build_estimator(names: list[str], *, manual_path: str, model: str = "claude-opus-5", effort: str = "high") -> CompositeEstimator:
    members: list[Estimator] = []
    for name in names:
        n = name.strip().lower()
        if n == "manual":
            members.append(ManualEstimator(manual_path))
        elif n == "microstructure":
            members.append(MicrostructureEstimator())
        elif n == "claude":
            try:
                members.append(ClaudeMarketEstimator(model=model, effort=effort))
            except ImportError:
                log.warning("estimator 'claude' requested but the anthropic package is not installed (pip install 'kalshitrader[ai]')")
        else:
            log.warning("unknown estimator %r ignored", name)
    if not members:
        members.append(MicrostructureEstimator())
    return CompositeEstimator(members)
