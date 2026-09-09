"""Claude analyst: turns a Kalshi screenshot (or a market payload) into a structured read.

This is the "visual extraction" half of the mandate. It returns a
`ScreenAnalysis` that the quant engine can score with the same EV/risk rules
it applies to API data, so screenshots and API markets flow through one
pipeline.

Requires `pip install 'kalshitrader[ai]'` and ANTHROPIC_API_KEY (or an `ant auth login` profile).
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kalshitrader.kalshi.models import Market, Orderbook

log = logging.getLogger(__name__)

from kalshitrader.tennis.analyst import api_json_schema  # noqa: E402  (shared schema scrubber)

SYSTEM_PROMPT = """You are a cold, low-risk quantitative prediction-market analyst reading Kalshi event contracts.

From the material provided, extract and verify:
1. Contract title and settlement criteria: wording, strike level, settlement source, close/expiry window.
2. Order book and pricing: current Yes/No bid, ask and last traded price in cents (market-implied probability).
3. Liquidity and spread: bid-ask gap and resting depth, for slippage risk.
4. Volume and momentum: recent tick direction and volume anomalies.

Then give your own probability that the contract settles YES (p_true), independent of the market price,
with a confidence in [0, 1] reflecting how much verifiable information supports it. Capital preservation is
the default: when the evidence is thin, keep confidence low so the engine PASSes. Never invent numbers you
cannot see; use null for anything not visible."""


class ScreenAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str | None = Field(None, description="Market ticker if visible, e.g. KXBTC-25SEP08-T60000")
    title: str = Field(..., description="Contract title as shown")
    settlement_criteria: str = Field(..., description="Strike/threshold, settlement source, resolution window")
    close_time: str | None = Field(None, description="ISO-8601 close or expiry time if visible")
    yes_bid: int | None = Field(None, description="0-100 cents")
    yes_ask: int | None = Field(None, description="0-100 cents")
    no_bid: int | None = Field(None, description="0-100 cents")
    no_ask: int | None = Field(None, description="0-100 cents")
    last_price: int | None = Field(None, description="0-100 cents")
    volume: int | None = Field(None, description="contracts traded")
    depth_at_ask: int | None = Field(None, description="Contracts resting at the Yes ask if visible")
    momentum: str = Field(..., description="up / down / flat / unknown, with a few words of evidence")
    p_true: float = Field(..., description="Your probability the contract settles YES, between 0 and 1")
    confidence: float = Field(..., description="How sure you are, between 0 and 1")
    rationale: str = Field(..., description="One sentence: the single strongest reason for p_true")

    @field_validator("p_true", "confidence", mode="after")
    @classmethod
    def _unit(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    def to_market(self) -> Market:
        yes_bid = self.yes_bid or (100 - self.no_ask if self.no_ask else 0)
        yes_ask = self.yes_ask or (100 - self.no_bid if self.no_bid else 0)
        return Market.from_api(
            {
                "ticker": self.ticker or "SCREENSHOT",
                "title": self.title,
                "rules_primary": self.settlement_criteria,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": self.no_bid or (100 - yes_ask if yes_ask else 0),
                "no_ask": self.no_ask or (100 - yes_bid if yes_bid else 0),
                "last_price": self.last_price or 0,
                "volume": self.volume or 0,
                "volume_24h": self.volume or 0,
                "close_time": self.close_time,
                "status": "open",
            }
        )


class ClaudeAnalyst:
    def __init__(self, model: str = "claude-opus-5", effort: str = "high", client=None, api_key: str | None = None):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - exercised via ImportError path in tests
                raise ImportError("install the AI extra: pip install 'kalshitrader[ai]'") from exc
            # Explicit key: a .env file does not reach os.environ, where the SDK looks.
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort

    # ---------------------------------------------------------------- core
    def _ask(self, content: list[dict]) -> ScreenAnalysis | None:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": api_json_schema(ScreenAnalysis)},
            },
            messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason == "refusal":
            log.warning("Claude declined the analysis request (stop_reason=refusal)")
            return None
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        if not text.strip():
            return None
        return ScreenAnalysis.model_validate_json(text)

    def analyze_screenshot(self, image_path: str | Path, extra_context: str = "") -> ScreenAnalysis | None:
        path = Path(image_path)
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        prompt = "Analyze this Kalshi screen capture." + (f" Context: {extra_context}" if extra_context else "")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": prompt},
        ]
        return self._ask(content)

    def analyze_market(self, market: Market, book: Orderbook | None) -> ScreenAnalysis | None:
        payload = {
            "ticker": market.ticker,
            "title": market.title,
            "subtitle": market.subtitle,
            "rules_primary": market.rules_primary,
            "close_time": market.close_time.isoformat() if market.close_time else None,
            "yes_bid": market.yes_bid,
            "yes_ask": market.yes_ask,
            "no_bid": market.no_bid,
            "no_ask": market.no_ask,
            "last_price": market.last_price,
            "volume_24h": market.volume_24h,
            "open_interest": market.open_interest,
            "orderbook": {"yes_bids": book.yes[:5], "no_bids": book.no[:5]} if book else None,
        }
        content = [{"type": "text", "text": "Analyze this Kalshi market payload:\n" + json.dumps(payload, indent=2, default=str)}]
        return self._ask(content)
